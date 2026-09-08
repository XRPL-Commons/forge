"""Release an idle builder or reviewer after a verified, published stage handoff.

The supervisor singleton serializes this with admission. This does not change
claims or PRs. Terminal quiescence alone never authorizes release.
"""
import argparse
from datetime import datetime
import hashlib
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


def bounded_run(run, seconds=30):
    """One proof gets one wall-clock budget, including nested remote reads."""
    deadline = time.monotonic() + seconds
    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise Unverified('verification_deadline_exceeded')
        return value
    def invoke(args, **kwargs):
        left = remaining()
        kwargs['timeout'] = min(kwargs.get('timeout', left), left)
        try:
            result = run(args, **kwargs)
        except subprocess.TimeoutExpired:
            raise Unverified('verification_deadline_exceeded')
        if time.monotonic() >= deadline:
            raise Unverified('verification_deadline_exceeded')
        return result
    invoke.remaining = remaining
    return invoke


def release_command(args, proof_run, action_run):
    # The proof must still be fresh before teardown. Once teardown reports
    # success, do not turn it into a refusal merely because the clock advanced.
    def run(args, **kwargs):
        kwargs['timeout'] = min(kwargs.get('timeout', 15), proof_run.remaining())
        return action_run(args, **kwargs)
    return command(args, run)


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


def session_identity(session, run, *, state=None):
    raw = command(['tmux', 'list-panes', '-s', '-t', '=' + session, '-F',
                   '#{session_name}|#{session_id}|#{session_created}|#{pane_id}|#{pane_pid}|#{pane_current_command}'], run)
    rows = raw.splitlines()
    if len(rows) != 1:
        raise Unverified('multiple_panes')
    values = rows[0].split('|')
    if (len(values) != 6 or values[0] != session or not re.fullmatch(r'\$\d+', values[1])
            or not values[2].isdigit() or not re.fullmatch(r'%\d+', values[3])
            or not values[4].isdigit() or values[5] not in {'claude', 'claude.exe', 'bash'}):
        raise Unverified('session_not_claude')
    pane = command(['tmux', 'capture-pane', '-e', '-p', '-t', values[3]], run)
    codex = values[5] == 'bash'
    if codex:
        if state is None:
            raise Unverified('codex_receipt_required')
        from .codex_worker import observe
        generation = '|'.join(values[1:5])
        observation = observe(state, session, generation=generation)
        if (observation.get('provider') != 'codex' or observation.get('reason') not in
                {'codex_execution_completed', 'codex_execution_failed'}):
            raise Unverified('codex_execution_not_quiescent')
        lines = [''.join(char for char, _ in line).strip() for line in styled_lines(pane)]
        lines = [line for line in lines if line]
        if not lines or not re.fullmatch(r'[\w.-]+@[\w.-]+:[^$#\r\n]*[$#]', lines[-1]):
            raise Unverified('not_empty_shell_prompt')
    elif not idle_prompt(pane):
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
    if (processes[root][1] != 'bash'
            or (codex and (len(descendants) != 1 or names != ['bash']))
            or (not codex and (len(descendants) != 2 or sorted(names) != ['bash', values[5]]))):
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
        schedule = snapshot.get('schedule', {})
        active_claims, handed = schedule.get('active'), schedule.get('handed_off')
        if (role != 'build' or type(counts.get('builder')) is not int or counts['builder'] < 0
                or not isinstance(active_claims, list) or not isinstance(handed, list)
                or any(type(n) is not int or n <= 0 for n in active_claims + handed)
                or not set(active_claims) <= set(handed) or schedule.get('reserving') != []
                or any(snapshot.get('work', {}).get(k) != [] for k in ('sentback', 'conflicts', 'limbo'))):
            raise Unverified('role_has_work')
        # New ready issues do not invalidate the old claim's canonical PR proof.
        # This only permits verification; it does not authorize a release itself.
    active = snapshot['schedule']['active']
    if role == 'build' and isinstance(active, list) and len(active) > 1:
        from .builder_handoff import check_multiple
        return check_multiple(repo, snapshot, state, session, clone, run, now, began,
                              read_command=command, identify_session=lambda s, r: session_identity(s, r, state=state), unverified=Unverified)
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
            branch = _checkout_branch(git, run)
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
            if (type(issue.get('number')) is int and issue['number'] == active[0]
                    and (identity or url)
                    and (not identity or isinstance(identity, str) and identity.lower() == repo.repo.lower())
                    and (not url or isinstance(url, str) and url.lower() == f'https://github.com/{repo.repo}/issues/{active[0]}'.lower())):
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
    identity = session_identity(session, run, state=state)
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
               and isinstance((pr.get('head') or {}).get('sha'), str)
               and ((pr.get('base') or {}).get('repo') or {}).get('full_name', '').lower() == repo.lower()]
    if len(matches) != 1:
        raise Unverified('detached_merged_pr_not_unique')
    pr = matches[0]
    merge = pr.get('merge_commit_sha')
    if not isinstance(merge, str) or not re.fullmatch(r'[0-9a-f]{40,64}', merge):
        raise Unverified('historical_commit_invalid')
    final_head = pr['head']['sha']
    if not re.fullmatch(r'[0-9a-f]{40,64}', final_head):
        raise Unverified('historical_commit_invalid')
    if final_head != head:
        # A reviewer may finish before later reviewed fixes advance the PR.
        # Prove its exact commit was retained in the final canonical PR history.
        comparison = json.loads(command(['gh', 'api', f'repos/{repo}/compare/{head}...{final_head}',
            '--jq', '{status,base_commit:{sha:.base_commit.sha},behind_by}'], run))
        if (comparison.get('status') not in {'ahead', 'identical'}
                or comparison.get('base_commit', {}).get('sha') != head
                or comparison.get('behind_by') != 0):
            raise Unverified('historical_head_not_in_merged_pr')
        return {'basis': 'ancestor_of_verified_merged_pr_head', 'branch': None,
                'pr': pr['number'], 'head': head, 'final_head': final_head, 'merge': merge}
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
    if ((role != 'build' and snapshot['counts'].get(role) != 0)
            or (role != 'build' and snapshot['work'].get(role) != [])
            or (role == 'build' and (type(snapshot['counts'].get('builder')) is not int
                or snapshot['counts']['builder'] < 0
                or any(snapshot['work'].get(k) != [] for k in ('sentback', 'conflicts', 'limbo'))))):
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
                                 '--label', 'changes-requested' if role == 'build' else 'ready-for-' + role, '--limit', '11', '--json', 'number,labels,isDraft'], run))
    if not isinstance(queued, list) or len(queued) > 10:
        raise Unverified('live_role_inventory_unbounded')
    for item in queued:
        labels = item.get('labels')
        if not isinstance(labels, list) or any(not isinstance(label, dict) or not isinstance(label.get('name'), str) for label in labels):
            raise Unverified('live_role_inventory_unknown')
        if 'hold' not in {label['name'] for label in labels}:
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
    identity = session_identity(session, run, state=state)
    return {'eligible': True, 'reason': 'verified_' + role + '_complete', 'evidence': {
        'repo': repo.repo, 'role': role, 'clone': str(clone), 'head': head,
        'published_main': main, 'branch': branch,
        'checkout_basis': ('merged_pr_history_on_published_main' if checkout and checkout.get('basis') == 'ancestor_of_verified_merged_pr_head'
                           else 'merged_pr_tree_on_published_main' if checkout else 'published_main_ancestor'),
        **({'checkout': {**checkout, 'verified_bases': [main]}} if checkout else {}), **identity}}


def _review_rejection(config, state, session, run, now):
    """An authored, current-head rejection is a completed reviewer handoff."""
    began=time.monotonic()
    matches=[r for r in load_repositories(config) if r.enabled and r.short+'-review'==session]
    if len(matches)!=1:raise Unverified('not_enabled_reviewer')
    repo=matches[0]
    path=snapshot_path(state,repo.repo);snapshot=json.loads(path.read_text())
    at=snapshot.get('observed_at')
    if (snapshot.get('repo')!=repo.repo or snapshot.get('status')!='ok' or snapshot.get('enabled') is not True
            or type(at) not in (int,float) or not math.isfinite(at) or not 0<=now-at<=300):
        raise Unverified('snapshot_not_fresh')
    if snapshot.get('counts',{}).get('review')!=0 or snapshot.get('work',{}).get('review')!=[]:
        raise Unverified('review_queue_not_empty')
    candidates=snapshot.get('work',{}).get('sentback',[])
    active=snapshot.get('schedule',{}).get('active',[])
    if (not isinstance(candidates,list) or not candidates or len(candidates)>10
            or not isinstance(active,list) or not active
            or any(type(n) is not int or n<=0 for n in candidates+active)):
        raise Unverified('no_bounded_rejection_queue')
    # A fresh full queue read prevents a stale empty review snapshot freeing a
    # reviewer that has newly assigned review work.
    queued=json.loads(command(['gh','pr','list','--repo',repo.repo,'--state','open',
        '--label','ready-for-review','--limit','11','--json','number,labels,isDraft'],run))
    if not isinstance(queued,list) or queued:raise Unverified('live_review_queue_not_empty')
    clone=Path(repo.clone_review or Path.home()/(repo.short+'-review')).resolve()
    git=['git','-C',str(clone)]
    if Path(command(git+['rev-parse','--show-toplevel'],run)).resolve()!=clone:raise Unverified('clone_mismatch')
    remote=command(git+['remote','get-url','origin'],run)
    if remote not in {f'git@github.com:{repo.repo}.git',f'https://github.com/{repo.repo}.git',f'https://github.com/{repo.repo}'}:
        raise Unverified('remote_mismatch')
    if command(git+['status','--porcelain=v1','--untracked-files=all'],run):raise Unverified('worktree_not_clean')
    head=command(git+['rev-parse','HEAD'],run);branch=_checkout_branch(git,run)
    if not re.fullmatch('[0-9a-f]{40,64}',head):raise Unverified('invalid_head')
    identity=session_identity(session,run,state=state)
    verified=[]
    for number in sorted(set(candidates)):
        pr=json.loads(command(['gh','pr','view',str(number),'--repo',repo.repo,'--json',
            'number,url,state,isDraft,headRefOid,baseRefOid,closingIssuesReferences,labels,author,reviews'],run))
        labels={label['name'] for label in pr['labels']}
        if (pr.get('number')!=number or pr.get('url','').lower()!=f'https://github.com/{repo.repo}/pull/{number}'.lower()
                or pr.get('state')!='OPEN' or pr.get('isDraft') is not False
                or pr.get('author',{}).get('login','').lower()!=repo.author.lower()
                or labels & {'ready-for-review','ready-for-audit','changes-requested'}!={'changes-requested'}
                or 'hold' in labels or pr.get('headRefOid')!=head
                or not re.fullmatch('[0-9a-f]{40,64}',pr.get('baseRefOid',''))):continue
        references=pr.get('closingIssuesReferences')
        if not isinstance(references,list) or not references or len(references)>20:continue
        numbers=[];valid=True
        for ref in references:
            n=ref.get('number');linked_repo=ref.get('repository',{}).get('nameWithOwner');url=ref.get('url')
            if (type(n) is not int or n not in active or not (linked_repo or url)
                    or linked_repo and linked_repo.lower()!=repo.repo.lower()
                    or url and url.lower()!=f'https://github.com/{repo.repo}/issues/{n}'.lower()):valid=False;break
            numbers.append(n)
        if not valid:continue
        reviews=pr.get('reviews')
        if not isinstance(reviews,list) or len(reviews)>100:raise Unverified('review_inventory_unbounded')
        authored=[]
        for review in reviews:
            if review.get('author',{}).get('login','').lower()!=repo.author.lower():continue
            try:submitted=datetime.fromisoformat(review['submittedAt'].replace('Z','+00:00')).timestamp()
            except (KeyError,ValueError,TypeError):raise Unverified('review_time_unknown')
            authored.append((submitted,review))
        if not authored:continue
        submitted,review=max(authored,key=lambda value:value[0])
        if sum(at==submitted for at,_ in authored)!=1:continue
        body=review.get('body','')
        if (not isinstance(body,str) or not identity['session_created']<=submitted<=now
                or review.get('commit',{}).get('oid')!=head or not review.get('id')):continue
        # COMMENTED is required for an account reviewing its own PR. Match only
        # a current-head review heading, never quoted findings or task prose.
        explicit=bool(re.match(r'(?i)^(?:correctness\s+)?review of\s+`?'+re.escape(head)+r'`?:\s*changes requested\b',body.strip()))
        if review.get('state')!='CHANGES_REQUESTED' and not (review.get('state')=='COMMENTED' and explicit):continue
        verified.append({'pr':number,'issues':sorted(set(numbers)),'pr_head':head,'pr_base':pr['baseRefOid'],
                         'review_id':review['id'],'review_at':submitted,
                         'review_digest':hashlib.sha256(body.encode()).hexdigest()})
    if len(verified)!=1:raise Unverified('current_reviewer_rejection_not_unique')
    if json.loads(path.read_text())!=snapshot:raise Unverified('snapshot_changed')
    if now+time.monotonic()-began-at>300:raise Unverified('snapshot_expired')
    if (command(git+['remote','get-url','origin'],run)!=remote
            or command(git+['rev-parse','HEAD'],run)!=head or _checkout_branch(git,run)!=branch
            or command(git+['status','--porcelain=v1','--untracked-files=all'],run)):
        raise Unverified('rejection_checkout_changed')
    if session_identity(session,run,state=state)!=identity:raise Unverified('session_changed')
    return {'eligible':True,'reason':'verified_handoff','evidence':{
        'repo':repo.repo,'role':'review','clone':str(clone),'head':head,'branch':branch,
        'checkout_basis':'current_pr_head_reviewer_rejection',**verified[0],**identity}}


def _tracked_completion_assignment(state, session):
    from .workflow import read_state
    view=read_state(state)
    if view['status']=='uninitialized':return None
    slot=next((row for row in view['slots'] if row['session']==session),None)
    if slot and slot.get('bootstrap') and slot.get('assignment_id') is None:return None
    assignment=next((row for row in view['assignments'] if slot and row['id']==slot.get('assignment_id')),None)
    if (not assignment or assignment['session']!=session or assignment['role'] not in {'review','audit'}
            or assignment.get('submitted_at') is None or assignment['state'] not in {'submitted','working','blocked','handoff'}
            or not assignment.get('pr') or not assignment.get('generation')
            or slot.get('generation')!=assignment['generation'] or slot.get('role')!=assignment['role']
            or slot.get('repo','').lower()!=assignment['repo'].lower()):
        raise Unverified('exact_completion_assignment_required')
    return assignment


def _tracked_role_completion(config,state,session,assignment,run,now):
    """Complete only this assigned PR, never the repository's unrelated queue."""
    began=time.monotonic();role=assignment['role']
    matches=[r for r in load_repositories(config) if r.enabled and r.short+'-'+role==session
             and r.repo.lower()==assignment['repo'].lower()]
    if len(matches)!=1:raise Unverified('not_enabled_worker')
    repo=matches[0];path=snapshot_path(state,repo.repo);snapshot=json.loads(path.read_text());at=snapshot.get('observed_at')
    if (snapshot.get('repo')!=repo.repo or snapshot.get('status')!='ok' or snapshot.get('enabled') is not True
            or type(at) not in (int,float) or not math.isfinite(at) or not 0<=now-at<=300):raise Unverified('snapshot_not_fresh')
    identity=session_identity(session,run,state=state)
    generation='|'.join(str(identity[k]) for k in ('session_id','session_created','pane_id','pane_pid'))
    if generation!=assignment['generation']:raise Unverified('assignment_generation_changed')
    clone=Path(getattr(repo,'clone_'+role) or Path.home()/(repo.short+'-'+role)).resolve();git=['git','-C',str(clone)]
    if Path(command(git+['rev-parse','--show-toplevel'],run)).resolve()!=clone:raise Unverified('clone_mismatch')
    remote=command(git+['remote','get-url','origin'],run)
    if remote not in {f'git@github.com:{repo.repo}.git',f'https://github.com/{repo.repo}.git',f'https://github.com/{repo.repo}'}:raise Unverified('remote_mismatch')
    if command(git+['status','--porcelain=v1','--untracked-files=all'],run):raise Unverified('worktree_not_clean')
    head=command(git+['rev-parse','HEAD'],run);branch=_checkout_branch(git,run)
    if not re.fullmatch('[0-9a-f]{40,64}',head):raise Unverified('invalid_head')
    number=assignment['pr']['number']
    pr=json.loads(command(['gh','pr','view',str(number),'--repo',repo.repo,'--json',
        'number,url,state,isDraft,headRefOid,baseRefOid,mergeCommit,mergedAt,closingIssuesReferences,labels,author,reviews'],run))
    labels={label['name'] for label in pr['labels']}
    if (pr.get('number')!=number or pr.get('url','').lower()!=f'https://github.com/{repo.repo}/pull/{number}'.lower()
            or pr.get('author',{}).get('login','').lower()!=repo.author.lower() or pr.get('isDraft') is not False
            or pr.get('headRefOid')!=assignment['pr']['head'] or 'hold' in labels):raise Unverified('assigned_pr_changed')
    refs=pr.get('closingIssuesReferences');numbers=[]
    if not isinstance(refs,list) or not refs or len(refs)>20:raise Unverified('assigned_pr_links_unknown')
    for ref in refs:
        n=ref.get('number');r=ref.get('repository',{}).get('nameWithOwner');url=ref.get('url')
        if (type(n) is not int or n<=0 or not (r or url) or r and r.lower()!=repo.repo.lower()
                or url and url.lower()!=f'https://github.com/{repo.repo}/issues/{n}'.lower()):raise Unverified('assigned_pr_links_changed')
        numbers.append(n)
    if sorted(set(numbers))!=assignment['issue_numbers']:raise Unverified('assigned_pr_links_changed')
    evidence={'assignment_id':assignment['id'],'pr':number,'issues':assignment['issue_numbers'],
              'pr_head':pr['headRefOid'],'pr_base':pr.get('baseRefOid'),'assigned_base':assignment['pr']['base']}
    if pr.get('state')=='MERGED':
        merge=(pr.get('mergeCommit') or {}).get('oid')
        try:merged_at=datetime.fromisoformat(pr['mergedAt'].replace('Z','+00:00')).timestamp()
        except (KeyError,ValueError,TypeError):raise Unverified('assigned_merge_time_unknown')
        if (not isinstance(merge,str) or not re.fullmatch('[0-9a-f]{40,64}',merge)
                or not max(identity['session_created'],assignment['submitted_at'])<=merged_at<=now):raise Unverified('assigned_merge_unverified')
        published=head
        if head==pr['headRefOid']:
            local_tree=command(git+['rev-parse',head+'^{tree}'],run)
            commit=json.loads(command(['gh','api',f'repos/{repo.repo}/git/commits/{merge}'],run))
            if commit.get('sha')!=merge or (commit.get('tree') or {}).get('sha')!=local_tree:raise Unverified('assigned_merged_tree_changed')
            published=merge
        elif head!=merge and branch!='main':raise Unverified('assigned_checkout_not_published')
        main=json.loads(command(['gh','api',f'repos/{repo.repo}/git/ref/heads/main'],run))['object']['sha']
        if not isinstance(main,str) or not re.fullmatch('[0-9a-f]{40,64}',main):raise Unverified('invalid_published_main')
        comparison=json.loads(command(['gh','api',f'repos/{repo.repo}/compare/{published}...{main}',
            '--jq','{status,base_commit:{sha:.base_commit.sha},behind_by}'],run))
        if (comparison.get('status') not in {'ahead','identical'} or comparison.get('base_commit',{}).get('sha')!=published
                or comparison.get('behind_by')!=0):raise Unverified('assigned_merge_not_on_main')
        evidence.update(checkout_basis='assigned_merged_pr_on_main',merge=merge,published_main=main)
    else:
        stages=labels & {'ready-for-review','ready-for-audit','changes-requested'}
        permitted=({'ready-for-audit'},{'changes-requested'}) if role=='review' else ({'changes-requested'},)
        if (pr.get('state')!='OPEN' or stages not in permitted
                or not isinstance(pr.get('baseRefOid'),str) or not re.fullmatch('[0-9a-f]{40,64}',pr['baseRefOid'])
                or head!=assignment['pr']['head']):raise Unverified('assigned_step_not_complete')
        rejection=stages=={'changes-requested'}
        reviews=pr.get('reviews')
        if not isinstance(reviews,list) or len(reviews)>100:raise Unverified('review_inventory_unbounded')
        authored=[]
        for review in reviews:
            if review.get('author',{}).get('login','').lower()!=repo.author.lower():continue
            try:submitted=datetime.fromisoformat(review['submittedAt'].replace('Z','+00:00')).timestamp()
            except (KeyError,ValueError,TypeError):raise Unverified('review_time_unknown')
            authored.append((submitted,review))
        if not authored:raise Unverified('assigned_step_record_missing')
        submitted,review=max(authored,key=lambda pair:pair[0]);body=review.get('body','')
        if (sum(at==submitted for at,_ in authored)!=1 or not isinstance(body,str) or not review.get('id')
                or review.get('commit',{}).get('oid')!=assignment['pr']['head']
                or not max(identity['session_created'],assignment['submitted_at'])<=submitted<=now):raise Unverified('assigned_step_record_changed')
        if role=='audit':
            marked=bool(re.match(r'(?i)^(?:#{1,6}\s*)?(?:\*{1,2})?(?:security audit|auditor|audit)\s*[:—-]\s*FAIL\b',body.strip()))
            if not (marked and assignment['pr']['head'] in body and assignment['pr']['base'] in body
                    and review.get('state') in {'COMMENTED','CHANGES_REQUESTED'}):raise Unverified('assigned_audit_rejection_unverified')
        else:
            outcome=r'(?:changes requested|FAIL)' if rejection else r'(?:PASS|approved)'
            marked=bool(re.match(r'(?i)^(?:(?:correctness\s+)?review of\s+`?'+re.escape(pr['headRefOid'])+r'`?:\s*'+outcome+
                r'\b|(?:#{1,6}\s*)?(?:\*{1,2})?review(?:er)?\s*[:—-]\s*'+outcome+r'\b)',body.strip()))
            if review.get('state')!=('CHANGES_REQUESTED' if rejection else 'APPROVED') and not (review.get('state')=='COMMENTED' and marked):raise Unverified('assigned_review_result_unverified')
        evidence.update(checkout_basis='assigned_current_pr_head_'+('rejection' if rejection else 'review_pass'),
            review_id=review['id'],review_at=submitted,review_digest=hashlib.sha256(body.encode()).hexdigest())
    if json.loads(path.read_text())!=snapshot:raise Unverified('snapshot_changed')
    if now+time.monotonic()-began-at>300:raise Unverified('snapshot_expired')
    if (command(git+['remote','get-url','origin'],run)!=remote or command(git+['rev-parse','HEAD'],run)!=head
            or _checkout_branch(git,run)!=branch or command(git+['status','--porcelain=v1','--untracked-files=all'],run)):
        raise Unverified('assigned_checkout_changed')
    if session_identity(session,run,state=state)!=identity:raise Unverified('session_changed')
    if _tracked_completion_assignment(state,session)!=assignment:raise Unverified('completion_assignment_changed')
    return {'eligible':True,'reason':'verified_handoff','evidence':{'repo':repo.repo,'role':role,'clone':str(clone),
        'head':head,'branch':branch,**evidence,**identity}}


def _check_with_fallback(config, state, session, run, now):
    if session.endswith("-build"):
        from .workflow_build_release import check as build_check
        result=build_check(config,state,session,run,now,unverified=Unverified,read_command=command,
            identify_session=lambda name,runner:session_identity(name,runner,state=state))
        if result is not None:return result
    if session.endswith(('-review','-audit')):
        assignment=_tracked_completion_assignment(state,session)
        if assignment is not None:return _tracked_role_completion(config,state,session,assignment,run,now)
    try:
        return _check(config, state, session, run, now)
    except Unverified as exc:
        if session.rsplit('-', 1)[-1] in {'build', 'review', 'audit'} and str(exc) in {
                'claim_not_unique', 'no_bounded_handoff_queue', 'published_handoff_not_unique', 'role_not_supported'}:
            if session.endswith('-review'):
                repo=next((r.repo for r in load_repositories(config)
                    if r.enabled and r.short+'-review'==session),None)
                if repo is None:raise Unverified('configuration_changed')
                snapshot=json.loads(snapshot_path(state,repo).read_text())
                if snapshot.get('work',{}).get('sentback'):
                    return _review_rejection(config,state,session,run,now)
            return _completed_role(config, state, session, run, now)
        raise


def check(config, state, session, *, run=subprocess.run, now=None):
    try:
        return _check_with_fallback(config, state, session, bounded_run(run), time.time() if now is None else now)
    except Unverified as exc:
        return {'eligible': False, 'reason': str(exc), 'evidence': {}}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, sqlite3.Error, subprocess.SubprocessError):
        return {'eligible': False, 'reason': 'verification_unavailable', 'evidence': {}}


def release(config, state, session, *, run=subprocess.run, now=None, lock_fd=8):
    """Require the inherited supervisor lock before any proof or teardown."""
    from .human_wait import _locked
    if not _locked(state, lock_fd):
        return {'eligible': False, 'released': False, 'reason': 'supervisor_lock_required', 'evidence': {}}
    action_run = run
    run = bounded_run(run, 60)
    first = check(config, state, session, run=run, now=now)
    if not first['eligible']:
        return {**first, 'released': False}
    second = check(config, state, session, run=run, now=now)
    if not second['eligible'] or first['evidence'] != second['evidence'] or not _locked(state, lock_fd):
        return {'eligible': False, 'released': False, 'reason': 'handoff_changed', 'evidence': {}}
    try:
        # IDs are never reused during a tmux server lifetime; target the verified
        # generation rather than a potentially replaced named session.
        from .workflow_lifecycle import prepare, confirmed
        workflow_ticket = prepare(state, session, second)
        release_command(['tmux', 'kill-session', '-t', second['evidence']['session_id']], run, action_run)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) and str(exc).startswith('workflow_') else 'release_failed'
        return {**second, 'released': False, 'reason': reason}
    result = {**second, 'released': True, 'observed_at': time.time() if now is None else now}
    result['workflow'] = confirmed(state, workflow_ticket)
    try:
        (Path(state) / (session + '.up')).unlink(missing_ok=True)
        atomic_json(Path(state) / 'handoffs' / (session + '.json'), result)
        from .activity import record
        record(state, 'session_stopped', session, repo=second['evidence']['repo'], role=second['evidence']['role'],
               metadata={'reason': second['reason']}, now=now)
        from .launch_budget import refund
        result['launch_budget_refund'] = refund(config, state, session=session, proof=result, now=now, lock_fd=lock_fd)
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
