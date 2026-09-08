"""Suppress idle auditor retries only for freshly verified human approval waits.

Read-only GitHub checks never authorize merge. Any uncertainty returns control to
normal auditing. Release additionally requires the inherited supervisor lock.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

from .config import load_repositories
from .control import atomic_json
from .handoff import Unverified, command, session_identity
from .issue_flow import _approval_action, _references
from .observability import classify_evidence


def _pages(endpoint, run):
    pages = json.loads(command(['gh', 'api', '--paginate', '--slurp', endpoint], run))
    if not isinstance(pages, list) or any(not isinstance(p, list) for p in pages):
        raise Unverified('invalid_remote_inventory')
    return [item for page in pages for item in page]


def _repository(config, repo):
    matches = [r for r in load_repositories(config) if r.repo == repo and r.enabled]
    if len(matches) != 1:
        raise Unverified('repository_not_enabled')
    return matches[0]


def _check(config, state, repo, run):
    repository = _repository(config, repo)
    ownership = {x['name'] for x in _pages(f'repos/{repo}/labels?per_page=100', run) if x['name'].startswith('auto:owned-by-')}
    if ownership != {repository.owner_label}:
        raise Unverified('ownership_uncertain')
    inventory = _pages(f'repos/{repo}/issues?state=open&labels=ready-for-audit&per_page=100', run)
    numbers = sorted({x['number'] for x in inventory if 'pull_request' in x})
    if not numbers or len(numbers) > 20:
        raise Unverified('no_bounded_audit_queue')
    candidates = []
    for number in numbers:
        live = json.loads(command(['gh', 'pr', 'view', str(number), '--repo', repo, '--json',
            'number,state,isDraft,headRefOid,baseRefOid,labels,author,reviewDecision,closingIssuesReferences'], run))
        labels = {x['name'] for x in live['labels']}
        if (live['number'] != number or live['state'] != 'OPEN' or live['isDraft']
                or 'critical' not in {s.lower() for s in labels}
                or 'ready-for-audit' not in labels or labels & {'hold', 'changes-requested', 'ready-for-review'}
                or live['reviewDecision'] not in ('REVIEW_REQUIRED', '', None)):
            raise Unverified('audit_candidate_actionable')
        if not all(isinstance(sha, str) and len(sha) in (40, 64)
                   and all(c in '0123456789abcdef' for c in sha)
                   for sha in (live['headRefOid'], live['baseRefOid'])):
            raise Unverified('audit_candidate_changed')
        # A missing aggregate is normal without a GitHub required-review rule.
        # Forge's CRITICAL policy still requires a human. Fetch the authoritative
        # authored audit now; a ten-minute history cache must not pin a worker.
        reviews = _pages(f'repos/{repo}/pulls/{number}/reviews?per_page=100', run)
        fresh_audits = [classify_evidence({'kind': 'review', 'id': review.get('id'),
                        'created_at': review.get('submitted_at'), 'commit_id': review.get('commit_id'),
                        'body': review.get('body') or '', 'url': review.get('html_url')})
                        for review in reviews
                        if (review.get('user') or {}).get('login') == repository.author
                        and review.get('state') == 'COMMENTED']
        data = {'number': number, 'state': 'open', 'labels': sorted(labels),
                'summary_status': 'ok', 'summary_head_sha': live['headRefOid'],
                'head_sha': live['headRefOid'], 'base_sha': live['baseRefOid'],
                'review_decision': live['reviewDecision'], 'evidence': fresh_audits}
        action = _approval_action(data, repo)
        if not action:
            raise Unverified('audit_evidence_changed')
        # A human-looking review resumes the auditor; it never grants merge.
        # Shared Forge identity does not count as a qualifying human approval.
        for review in reviews:
            login = (review.get('user') or {}).get('login')
            if review.get('state') == 'APPROVED' and login not in {repository.author, live['author']['login']}:
                if review.get('commit_id') in (None, live['headRefOid']):
                    raise Unverified('approval_requires_recheck')
            if review.get('state') == 'CHANGES_REQUESTED' and review.get('commit_id') == live['headRefOid']:
                raise Unverified('review_requires_recheck')
        current_refs = _references({'raw_pr_summary': live}, repo)
        if not current_refs:
            raise Unverified('current_issue_links_unavailable')
        candidates.append({'pr': number, 'head': live['headRefOid'], 'base': live['baseRefOid'],
                           'issues': sorted(current_refs), 'audit_url': action['evidence_url']})
    return {'eligible': True, 'reason': 'verified_awaiting_human_approval',
            'evidence': {'repo': repo, 'candidates': candidates}}


def check(config, state, repo, *, run=subprocess.run):
    try:
        return _check(config, state, repo, run)
    except Unverified as exc:
        return {'eligible': False, 'reason': str(exc), 'evidence': {}}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error, subprocess.SubprocessError):
        return {'eligible': False, 'reason': 'verification_unavailable', 'evidence': {}}


def _locked(state, fd):
    expected = Path(state).resolve().parent / 'state-supervisor.lock'
    try:
        actual = os.fstat(fd)
        target = expected.stat()
        if (actual.st_dev, actual.st_ino) != (target.st_dev, target.st_ino):
            return False
        # Inherited description must hold the lock already. A separate open must
        # fail, while a nonblocking lock on the inherited description succeeds.
        with expected.open('a') as probe:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            fcntl.flock(probe, fcntl.LOCK_UN)
    except (OSError, ValueError):
        return False
    return False


def _published_checkout(git, repo, head, bases, run):
    """Prove an old audit checkout is exactly a merged, reachable tree."""
    branch = command(git + ['symbolic-ref', '--quiet', '--short', 'HEAD'], run)
    if branch == 'main':
        # A clean old main is published history when reachable from every
        # freshly verified GitHub candidate base; no local remote ref is trusted.
        for base in sorted(bases):
            command(git + ['merge-base', '--is-ancestor', head, base], run)
        return {'basis': 'main_ancestor_of_verified_candidate_bases',
                'branch': branch, 'head': head, 'verified_bases': sorted(bases)}
    if not branch or command(git + ['config', '--get', f'branch.{branch}.remote'], run) != 'origin':
        raise Unverified('historical_branch_not_tracking_origin')
    tracking = command(git + ['config', '--get', f'branch.{branch}.merge'], run)
    if not tracking.startswith('refs/heads/') or not tracking.removeprefix('refs/heads/'):
        raise Unverified('historical_tracking_branch_unknown')
    remote_branch = tracking.removeprefix('refs/heads/')
    prs = json.loads(command(['gh', 'pr', 'list', '--repo', repo, '--state', 'merged',
                             '--head', remote_branch, '--limit', '11', '--json',
                             'number,headRefOid,headRefName,mergeCommit,state,mergedAt'], run))
    if not isinstance(prs, list) or len(prs) > 10:
        raise Unverified('historical_pr_search_unbounded')
    matches = [pr for pr in prs if pr.get('headRefOid') == head
               and pr.get('headRefName') == remote_branch and pr.get('state') == 'MERGED'
               and pr.get('mergedAt') and (pr.get('mergeCommit') or {}).get('oid')]
    if len(matches) != 1:
        raise Unverified('historical_published_pr_not_unique')
    pr = matches[0]
    merge = pr['mergeCommit']['oid']
    if not all(isinstance(sha, str) and len(sha) in (40, 64)
               and all(c in '0123456789abcdef' for c in sha) for sha in (head, merge)):
        raise Unverified('historical_commit_invalid')
    tree = command(git + ['rev-parse', head + '^{tree}'], run)
    local_merge = run(git + ['rev-parse', merge + '^{tree}'],
                      capture_output=True, text=True, timeout=15)
    if local_merge.returncode == 0:
        merge_tree = local_merge.stdout.strip()
    else:
        # Completed clones need not have fetched the squash/merge object.
        # Ask for this immutable canonical commit; never substitute branch tip.
        commit = json.loads(command(['gh', 'api', f'repos/{repo}/git/commits/{merge}'], run))
        if commit.get('sha') != merge:
            raise Unverified('historical_commit_identity_changed')
        merge_tree = (commit.get('tree') or {}).get('sha')
    if tree != merge_tree:
        raise Unverified('historical_checkout_differs_from_merged_tree')
    for base in sorted(bases):
        command(git + ['merge-base', '--is-ancestor', merge, base], run)
    return {'basis': 'exact_tree_of_verified_merged_pr', 'branch': branch,
            'remote_branch': remote_branch, 'pr': pr['number'], 'head': head,
            'merge': merge, 'tree': tree, 'verified_bases': sorted(bases)}


def _release_evidence(config, state, repo, session, run):
    result = check(config, state, repo, run=run)
    if not result['eligible']:
        raise Unverified(result['reason'])
    repository = _repository(config, repo)
    if session != repository.short + '-audit':
        raise Unverified('not_repository_auditor')
    clone = Path(repository.clone_audit or (Path.home() / (repository.short + '-audit'))).resolve()
    git = ['git', '-C', str(clone)]
    if Path(command(git + ['rev-parse', '--show-toplevel'], run)).resolve() != clone:
        raise Unverified('clone_mismatch')
    remote = command(git + ['remote', 'get-url', 'origin'], run)
    if remote not in {f'git@github.com:{repo}.git', f'https://github.com/{repo}.git', f'https://github.com/{repo}'}:
        raise Unverified('remote_mismatch')
    if command(git + ['status', '--porcelain=v1', '--untracked-files=all'], run):
        raise Unverified('worktree_not_clean')
    head = command(git + ['rev-parse', 'HEAD'], run)
    candidate_commits = {c[k] for c in result['evidence']['candidates'] for k in ('head', 'base')}
    checkout = {'basis': 'current_candidate_head_or_base', 'head': head}
    if head not in candidate_commits:
        checkout = _published_checkout(git, repo, head,
                    {c['base'] for c in result['evidence']['candidates']}, run)
    identity = session_identity(session, run)
    return {**result['evidence'], 'session': session, 'role': 'audit', 'clone': str(clone), 'head': head, 'checkout': checkout, **identity}


def _clear_verified_no_progress(state, session):
    """A verified policy wait corrects only this session's no-progress diagnosis."""
    directory = Path(state) / 'legacy-health'
    path = directory / 'guard.json'
    if not path.exists():
        return False
    # Share the guard's own lock as well as the caller's supervisor lock. Guard
    # observers can run independently and must not lose a concurrent update.
    with (directory / 'guard.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        guard = json.loads(path.read_text())
        record = guard.get('sessions', {}).get(session)
        if not isinstance(record, dict) or record.get('reason') != 'no_progress':
            return False
        record['until'] = 0
        record.pop('reason')
        atomic_json(path, guard)
        return True


def release(config, state, repo, *, session=None, lock_fd=8, run=subprocess.run):
    if not _locked(state, lock_fd):
        return {'eligible': False, 'released': False, 'reason': 'supervisor_lock_required', 'evidence': {}}
    try:
        session = session or _repository(config, repo).short + '-audit'
        first = _release_evidence(config, state, repo, session, run)
        second = _release_evidence(config, state, repo, session, run)
        if first != second or not _locked(state, lock_fd):
            raise Unverified('audit_wait_changed')
        command(['tmux', 'kill-session', '-t', second['session_id']], run)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError) as exc:
        return {'eligible': False, 'released': False,
                'reason': str(exc) if isinstance(exc, Unverified) else 'verification_unavailable', 'evidence': {}}
    result = {'eligible': True, 'released': True, 'reason': 'verified_awaiting_human_approval',
              'evidence': second, 'observed_at': time.time()}
    try:
        (Path(state) / (session + '.up')).unlink(missing_ok=True)
        result['cleared_no_progress'] = _clear_verified_no_progress(state, session)
        atomic_json(Path(state) / 'human-waits' / (session + '.json'), result)
        from .activity import record
        record(state, 'session_stopped', session, repo=repo, role='audit',
               metadata={'reason': result['reason'], 'candidates': second['candidates']})
        from .launch_budget import refund
        result['launch_budget_refund'] = refund(config, state, session=session, proof=result, lock_fd=lock_fd)
    except (OSError, ValueError, TypeError, AttributeError, ImportError, sqlite3.Error):
        result['receipt_error'] = True
    return result


def write_check_receipt(state, repo, result, *, now=None):
    """CLI observation only: overwrite on failure so prior waits cannot linger."""
    value = {**result, 'repo': repo, 'observed_at': time.time() if now is None else now}
    if len(json.dumps(value).encode()) > 65536:
        value = {'repo': repo, 'observed_at': value['observed_at'], 'eligible': False,
                 'reason': 'check_receipt_limit', 'evidence': {}}
    name = hashlib.sha256(repo.lower().encode()).hexdigest() + '.json'
    atomic_json(Path(state) / 'human-wait-checks' / name, value)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'release'))
    parser.add_argument('--repo', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--state', required=True)
    parser.add_argument('--session')
    parser.add_argument('--lock-fd', type=int, default=8)
    args = parser.parse_args()
    result = (release(args.config, args.state, args.repo, session=args.session, lock_fd=args.lock_fd)
              if args.action == 'release' else check(args.config, args.state, args.repo))
    if args.action == 'check':
        try:
            write_check_receipt(args.state, args.repo, result)
        except (OSError, ValueError):
            result['receipt_error'] = True
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get('released', result['eligible']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
