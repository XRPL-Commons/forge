"""Release an idle builder or reviewer after a verified, published stage handoff.

The supervisor singleton serializes this with admission. This does not change
claims or PRs. Terminal quiescence alone never authorizes release.
"""
import argparse
import json
import math
from pathlib import Path
import re
import sqlite3
import subprocess
import time

from .config import load_repositories
from .control import atomic_json, snapshot_path


class Unverified(ValueError):
    pass


def command(args, run):
    result = run(args, capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise Unverified('read_failed')
    return result.stdout.strip()


def styled_lines(pane):
    """Keep SGR faint information: Claude's suggested input is faint, typed input is not."""
    escape = re.compile(r'\x1b\[[0-9;]*m|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)')
    faint = False
    lines = [[]]
    position = 0
    for match in escape.finditer(pane):
        for char in pane[position:match.start()]:
            if char == '\n':
                lines.append([])
            else:
                lines[-1].append((char, faint))
        code = match.group()
        if code.startswith('\x1b['):
            params = [int(value or 0) for value in code[2:-1].split(';')]
            index = 0
            while index < len(params):
                value = params[index]
                if value in (38, 48, 58):
                    # RGB and indexed colour values are not text attributes.
                    index += 5 if params[index + 1:index + 2] == [2] else 3
                    continue
                if value in (0, 22):
                    faint = False
                elif value == 2:
                    faint = True
                index += 1
        position = match.end()
    for char in pane[position:]:
        if char == '\n':
            lines.append([])
        else:
            lines[-1].append((char, faint))
    return lines


def idle_prompt(pane):
    """Require empty input or a styled faint suggestion plus the idle footer.

    Plain text cannot distinguish a suggested continuation from unsent input.
    Nonempty unstyled input is always rejected; capture-pane must preserve SGR.
    """
    styled = styled_lines(pane)
    lines = [''.join(char for char, _ in line) for line in styled]
    if '\x1b' in ''.join(lines):
        return False  # Unrecognized terminal control sequence.
    prompts = [i for i, line in enumerate(lines) if re.match(r'\s*❯', line)]
    if not prompts:
        return False
    prompt = prompts[-1]
    marker = lines[prompt].index('❯')
    if any(not char.isspace() and not faint for char, faint in styled[prompt][marker + 1:]):
        return False
    footer = '\n'.join(lines[prompt + 1:]).lower()
    return ('bypass permissions on' in footer and 'esc to interrupt' not in footer
            and not re.search(r'allow|approve|permission required|yes.*no', footer))


def session_identity(session, run):
    raw = command(['tmux', 'list-panes', '-s', '-t', '=' + session, '-F',
                   '#{session_name}|#{session_id}|#{session_created}|#{pane_id}|#{pane_pid}|#{pane_current_command}'], run)
    rows = raw.splitlines()
    if len(rows) != 1:
        raise Unverified('multiple_panes')
    values = rows[0].split('|')
    if (len(values) != 6 or values[0] != session or not re.fullmatch(r'\$\d+', values[1])
            or not values[2].isdigit() or not re.fullmatch(r'%\d+', values[3])
            or not values[4].isdigit() or values[5] not in {'claude', 'claude.exe'}):
        raise Unverified('session_not_claude')
    pane = command(['tmux', 'capture-pane', '-e', '-p', '-t', values[3]], run)
    if not idle_prompt(pane):
        raise Unverified('not_explicitly_idle')
    # Read process names only, never potentially sensitive command arguments.
    processes = {}
    for line in command(['ps', '-eo', 'pid=,ppid=,comm='], run).splitlines():
        pid, parent, name = line.split(None, 2)
        processes[int(pid)] = (int(parent), name)
    root = int(values[4])
    descendants = {root}
    while True:
        expanded = descendants | {pid for pid, (parent, _) in processes.items() if parent in descendants}
        if expanded == descendants:
            break
        descendants = expanded
    names = [processes[pid][1] for pid in descendants]
    if (len(descendants) != 2 or processes[root][1] != 'bash'
            or sorted(names) != ['bash', values[5]]):
        raise Unverified('processes_not_quiescent')
    return dict(session_id=values[1], session_created=int(values[2]), pane_id=values[3],
                pane_pid=root, processes=sorted(descendants))


def _check(config, state, session, run, now):
    began = time.monotonic()
    repositories = load_repositories(config)
    role = session.rsplit('-', 1)[-1]
    if role not in {'build', 'review'}:
        raise Unverified('role_not_supported')
    matches = [r for r in repositories if r.enabled and r.short + '-' + role == session]
    if len(matches) != 1:
        raise Unverified('not_enabled_worker')
    repo = matches[0]
    clone = Path((repo.clone_build if role == 'build' else repo.clone_review)
                 or (Path.home() / (repo.short if role == 'build' else repo.short + '-review'))).resolve()
    snapshot = json.loads(snapshot_path(state, repo.repo).read_text())
    observed = snapshot.get('observed_at')
    if (snapshot.get('status') != 'ok' or snapshot.get('enabled') is not True
            or snapshot.get('repo') != repo.repo or type(observed) not in (int, float)
            or not math.isfinite(observed) or not 0 <= now - observed <= 300):
        raise Unverified('snapshot_not_fresh')
    counts = snapshot['counts']
    if counts.get('builder' if role == 'build' else 'review') != 0:
        raise Unverified('role_has_work')
    active = snapshot['schedule']['active']
    if role == 'build' and isinstance(active, list) and len(active) > 1:
        from .builder_handoff import check_multiple
        return check_multiple(repo, snapshot, state, session, clone, run, now, began,
                              read_command=command, identify_session=session_identity, unverified=Unverified)
    if len(active) != 1 or type(active[0]) is not int or active[0] <= 0:
        raise Unverified('claim_not_unique')
    work = snapshot['work']
    if role == 'review' and work['review']:
        raise Unverified('review_queue_not_empty')
    candidates = work['review'] + work['audit'] if role == 'build' else work['audit']
    if not candidates or len(candidates) > 10 or any(type(n) is not int or n <= 0 for n in candidates):
        raise Unverified('no_bounded_handoff_queue')
    git = ['git', '-C', str(clone)]
    if Path(command(git + ['rev-parse', '--show-toplevel'], run)).resolve() != clone:
        raise Unverified('clone_mismatch')
    remote = command(git + ['remote', 'get-url', 'origin'], run)
    if remote not in {f'git@github.com:{repo.repo}.git', f'https://github.com/{repo.repo}.git', f'https://github.com/{repo.repo}'}:
        raise Unverified('remote_mismatch')
    if command(git + ['status', '--porcelain=v1', '--untracked-files=all'], run):
        raise Unverified('worktree_not_clean')
    head = command(git + ['rev-parse', 'HEAD'], run)
    if not re.fullmatch(r'[0-9a-f]{40,64}', head):
        raise Unverified('invalid_head')
    verified = []
    for number in sorted(set(candidates)):
        pr = json.loads(command(['gh', 'pr', 'view', str(number), '--repo', repo.repo, '--json',
                                 'closingIssuesReferences,state,isDraft,headRefOid,baseRefOid,labels,author'], run))
        labels = {label['name'] for label in pr['labels']}
        on_pr_head = pr['headRefOid'] == head
        on_published_main = (pr['baseRefOid'] == head
                             and command(git + ['rev-parse', 'origin/main'], run) == head)
        on_stale_main = False
        if role == 'review' and not (on_pr_head or on_published_main):
            branch = command(git + ['symbolic-ref', '--short', 'HEAD'], run)
            if branch == 'main' and command(git + ['rev-parse', 'origin/main'], run) == pr['baseRefOid']:
                ancestor = run(git + ['merge-base', '--is-ancestor', head, pr['baseRefOid']],
                               capture_output=True, text=True, timeout=15)
                if ancestor.returncode not in (0, 1):
                    raise Unverified('ancestry_unavailable')
                on_stale_main = ancestor.returncode == 0
        if (pr['state'] != 'OPEN' or pr['isDraft'] is not False or not (on_pr_head or on_published_main or on_stale_main)
                or pr['author']['login'] != repo.author or 'hold' in labels
                or 'changes-requested' in labels
                or len(labels & {'ready-for-review', 'ready-for-audit'}) != 1
                or (role == 'review' and 'ready-for-audit' not in labels)):
            continue
        for issue in pr['closingIssuesReferences']:
            identity = issue.get('repository', {}).get('nameWithOwner')
            url = issue.get('url', '')
            if issue.get('number') == active[0] and (identity == repo.repo or url.lower() == f'https://github.com/{repo.repo}/issues/{active[0]}'.lower()):
                verified.append((number, pr['headRefOid'], pr['baseRefOid'],
                                 'pr_head' if on_pr_head else ('published_main' if on_published_main else 'published_main_ancestor')))
                break
    if len(verified) != 1:
        raise Unverified('published_handoff_not_unique')
    # PR reads may take time. Check local evidence again after the network read.
    refreshed = json.loads(snapshot_path(state, repo.repo).read_text())
    if refreshed != snapshot:
        raise Unverified('snapshot_changed')
    if now + (time.monotonic() - began) - observed > 300:
        raise Unverified('snapshot_expired')
    if command(git + ['status', '--porcelain=v1', '--untracked-files=all'], run):
        raise Unverified('worktree_not_clean')
    if command(git + ['rev-parse', 'HEAD'], run) != head:
        raise Unverified('head_changed')
    if verified[0][3] == 'published_main' and command(git + ['rev-parse', 'origin/main'], run) != head:
        raise Unverified('published_main_changed')
    if verified[0][3] == 'published_main_ancestor':
        if (command(git + ['symbolic-ref', '--short', 'HEAD'], run) != 'main'
                or command(git + ['rev-parse', 'origin/main'], run) != verified[0][2]):
            raise Unverified('published_main_changed')
    identity = session_identity(session, run)
    return {'eligible': True, 'reason': 'verified_handoff', 'evidence': {
        'repo': repo.repo, 'role': role, 'clone': str(clone), 'issue': active[0], 'pr': verified[0][0], 'pr_head': verified[0][1],
        'pr_base': verified[0][2], 'checkout_basis': verified[0][3],
        'branch': 'main' if verified[0][3] == 'published_main_ancestor' else None,
        'head': head, **identity}}


def _checkout_branch(git, run):
    result = run(git + ['symbolic-ref', '--short', 'HEAD'],
                 capture_output=True, text=True, timeout=15)
    if result.returncode == 0:
        return result.stdout.strip()
    if result.returncode == 1:  # Detached HEAD; publication still needs remote proof.
        return None
    # Without --quiet Git reports a detached HEAD as 128. Verify explicitly
    # with --quiet so other repository/read errors cannot masquerade as detached.
    result = run(git + ['symbolic-ref', '--quiet', '--short', 'HEAD'],
                 capture_output=True, text=True, timeout=15)
    if result.returncode == 1:
        return None
    raise Unverified('branch_unavailable')


def _detached_merged_checkout(git, repo, head, run):
    prs = json.loads(command(['gh', 'api', f'repos/{repo}/commits/{head}/pulls?per_page=11'], run))
    if not isinstance(prs, list) or len(prs) > 10:
        raise Unverified('detached_pr_search_unbounded')
    matches = [pr for pr in prs if pr.get('state') == 'closed' and pr.get('merged_at')
               and (pr.get('head') or {}).get('sha') == head
               and ((pr.get('base') or {}).get('repo') or {}).get('full_name', '').lower() == repo.lower()]
    if len(matches) != 1:
        raise Unverified('detached_merged_pr_not_unique')
    pr = matches[0]
    merge = pr.get('merge_commit_sha')
    if not isinstance(merge, str) or not re.fullmatch(r'[0-9a-f]{40,64}', merge):
        raise Unverified('historical_commit_invalid')
    commit = json.loads(command(['gh', 'api', f'repos/{repo}/git/commits/{merge}'], run))
    tree = command(git + ['rev-parse', head + '^{tree}'], run)
    if commit.get('sha') != merge or (commit.get('tree') or {}).get('sha') != tree:
        raise Unverified('historical_checkout_differs_from_merged_tree')
    return {'basis': 'exact_tree_of_verified_merged_pr', 'branch': None,
            'pr': pr['number'], 'head': head, 'merge': merge, 'tree': tree}


def _completed_role(config, state, session, run, now):
    """Completed review/audit roles remain reclaimable after their PR/claim disappears."""
    began = time.monotonic()
    role = session.rsplit('-', 1)[-1]
    if role not in {'build', 'review', 'audit'}:
        raise Unverified('role_not_supported')
    matches = [r for r in load_repositories(config) if r.enabled and r.short + '-' + role == session]
    if len(matches) != 1:
        raise Unverified('not_enabled_worker')
    repo = matches[0]
    snapshot = json.loads(snapshot_path(state, repo.repo).read_text())
    observed = snapshot.get('observed_at')
    if (snapshot.get('status') != 'ok' or snapshot.get('enabled') is not True
            or snapshot.get('repo') != repo.repo or type(observed) not in (int, float)
            or not math.isfinite(observed) or not 0 <= now - observed <= 300):
        raise Unverified('snapshot_not_fresh')
    if (snapshot['counts'].get('builder' if role == 'build' else role) != 0
            or (role != 'build' and snapshot['work'].get(role) != [])
            or (role == 'build' and any(snapshot['work'].get(k) != [] for k in ('ready', 'sentback', 'conflicts', 'limbo')))):
        raise Unverified('role_queue_not_empty')
    clone = Path((repo.clone_build if role == 'build' else repo.clone_review if role == 'review' else repo.clone_audit)
                 or Path.home() / (repo.short if role == 'build' else repo.short + '-' + role)).resolve()
    git = ['git', '-C', str(clone)]
    if Path(command(git + ['rev-parse', '--show-toplevel'], run)).resolve() != clone:
        raise Unverified('clone_mismatch')
    remote = command(git + ['remote', 'get-url', 'origin'], run)
    if remote not in {f'git@github.com:{repo.repo}.git', f'https://github.com/{repo.repo}.git', f'https://github.com/{repo.repo}'}:
        raise Unverified('remote_mismatch')
    branch = _checkout_branch(git, run)
    if command(git + ['status', '--porcelain=v1', '--untracked-files=all'], run):
        raise Unverified('worktree_not_clean')
    head = command(git + ['rev-parse', 'HEAD'], run)
    if not re.fullmatch(r'[0-9a-f]{40,64}', head):
        raise Unverified('invalid_head')
    queued = json.loads(command(['gh', 'pr', 'list', '--repo', repo.repo, '--state', 'open',
                                 '--label', 'changes-requested' if role == 'build' else 'ready-for-' + role, '--limit', '1', '--json', 'number'], run))
    if queued != []:
        raise Unverified('live_role_queue_not_empty')
    main = json.loads(command(['gh', 'api', f'repos/{repo.repo}/git/ref/heads/main'], run))['object']['sha']
    if not isinstance(main, str) or not re.fullmatch(r'[0-9a-f]{40,64}', main):
        raise Unverified('invalid_published_main')
    checkout = None
    if role == 'build' and branch in ('main', None):
        raise Unverified('builder_completed_pr_required')
    published = head
    if branch not in ('main', None):
        # Reuse the exact merged-PR tree proof. Verify ancestry remotely below,
        # so a missing local object for today's main tip does not require fetch.
        from .human_wait import _published_checkout
        checkout = _published_checkout(git, repo.repo, head, set(), run)
        if checkout.get('branch') != branch or checkout.get('basis') != 'exact_tree_of_verified_merged_pr':
            raise Unverified('historical_checkout_changed')
        published = checkout['merge']
    if branch is None and head != main:
        checkout = _detached_merged_checkout(git, repo.repo, head, run)
        published = checkout['merge']
    comparison = json.loads(command(['gh', 'api', f'repos/{repo.repo}/compare/{published}...{main}',
                                      '--jq', '{status,base_commit:{sha:.base_commit.sha},behind_by}'], run))
    if (comparison.get('status') not in {'ahead', 'identical'}
            or comparison.get('base_commit', {}).get('sha') != published
            or comparison.get('behind_by') != 0):
        raise Unverified('head_not_published_main_ancestor')
    # This proof does not need a local object for the new main tip or a fetch.
    if json.loads(snapshot_path(state, repo.repo).read_text()) != snapshot:
        raise Unverified('snapshot_changed')
    if now + time.monotonic() - began - observed > 300:
        raise Unverified('snapshot_expired')
    if (_checkout_branch(git, run) != branch
            or command(git + ['rev-parse', 'HEAD'], run) != head
            or command(git + ['remote', 'get-url', 'origin'], run) != remote):
        raise Unverified('checkout_changed')
    if command(git + ['status', '--porcelain=v1', '--untracked-files=all'], run):
        raise Unverified('worktree_not_clean')
    identity = session_identity(session, run)
    return {'eligible': True, 'reason': 'verified_' + role + '_complete', 'evidence': {
        'repo': repo.repo, 'role': role, 'clone': str(clone), 'head': head,
        'published_main': main, 'branch': branch,
        'checkout_basis': 'merged_pr_tree_on_published_main' if checkout else 'published_main_ancestor',
        **({'checkout': {**checkout, 'verified_bases': [main]}} if checkout else {}), **identity}}


def _check_with_fallback(config, state, session, run, now):
    try:
        return _check(config, state, session, run, now)
    except Unverified as exc:
        if session.rsplit('-', 1)[-1] in {'build', 'review', 'audit'} and str(exc) in {
                'claim_not_unique', 'no_bounded_handoff_queue', 'published_handoff_not_unique', 'role_not_supported'}:
            return _completed_role(config, state, session, run, now)
        raise


def check(config, state, session, *, run=subprocess.run, now=None):
    try:
        return _check_with_fallback(config, state, session, run, time.time() if now is None else now)
    except Unverified as exc:
        return {'eligible': False, 'reason': str(exc), 'evidence': {}}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, subprocess.SubprocessError):
        return {'eligible': False, 'reason': 'verification_unavailable', 'evidence': {}}


def release(config, state, session, *, run=subprocess.run, now=None):
    """Caller holds the supervisor singleton lock; every release checks twice."""
    first = check(config, state, session, run=run, now=now)
    if not first['eligible']:
        return {**first, 'released': False}
    second = check(config, state, session, run=run, now=now)
    if not second['eligible'] or first['evidence'] != second['evidence']:
        return {'eligible': False, 'released': False, 'reason': 'handoff_changed', 'evidence': {}}
    try:
        # IDs are never reused during a tmux server lifetime; target the verified
        # generation rather than a potentially replaced named session.
        command(['tmux', 'kill-session', '-t', second['evidence']['session_id']], run)
    except (OSError, ValueError, subprocess.SubprocessError):
        return {**second, 'released': False, 'reason': 'release_failed'}
    result = {**second, 'released': True, 'observed_at': time.time() if now is None else now}
    try:
        (Path(state) / (session + '.up')).unlink(missing_ok=True)
        atomic_json(Path(state) / 'handoffs' / (session + '.json'), result)
        from .activity import record
        record(state, 'session_stopped', session, repo=second['evidence']['repo'], role=second['evidence']['role'],
               metadata={'reason': second['reason']}, now=now)
        from .launch_budget import refund
        result['launch_budget_refund'] = refund(config, state, session=session, proof=result, now=now)
    except (OSError, ValueError, ImportError, sqlite3.Error):
        result['receipt_error'] = True
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'release'))
    parser.add_argument('--config', required=True)
    parser.add_argument('--state', required=True)
    parser.add_argument('--session', required=True)
    args = parser.parse_args()
    value = (release if args.action == 'release' else check)(args.config, args.state, args.session)
    print(json.dumps(value, sort_keys=True))
    return 0 if value.get('released', value['eligible']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
