"""Bounded proof that every builder claim has an exact published PR handoff."""
import json
from pathlib import Path
import re
import time

from .control import snapshot_path


def check_multiple(repo, snapshot, state, session, clone, run, now, began, *,
                   read_command, identify_session, unverified):
    """Called only after enabled configuration and fresh zero-work snapshot checks.

    Every claim needs one canonical PR; quiet terminals, titles, issue mentions,
    and the presence of an arbitrary published PR are insufficient. Distinct
    multi_claim errors deliberately cannot enter completed-role fallback logic.
    """
    command, session_identity, Unverified = read_command, identify_session, unverified
    active = snapshot['schedule']['active']
    if (not isinstance(active, list) or not 2 <= len(active) <= 10
            or any(type(n) is not int or n <= 0 for n in active)
            or len(set(active)) != len(active)):
        raise Unverified('multi_claim_inventory_invalid')
    work = snapshot['work']
    if 'wip' in work and (not isinstance(work['wip'], list)
            or any(type(n) is not int for n in work['wip'])
            or set(work['wip']) != set(active)):
        raise Unverified('multi_claim_inventory_changed')
    candidates = work['review'] + work['audit']
    if (not candidates or len(candidates) > 10
            or any(type(n) is not int or n <= 0 for n in candidates)
            or len(set(candidates)) != len(candidates)):
        raise Unverified('multi_claim_queue_invalid')
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
    covered = {n: [] for n in active}
    proofs = []
    for number in sorted(candidates):
        pr = json.loads(command(['gh', 'pr', 'view', str(number), '--repo', repo.repo, '--json',
            'closingIssuesReferences,state,isDraft,headRefOid,baseRefOid,labels,author'], run))
        labels = {label['name'] for label in pr['labels']}
        if (pr['state'] != 'OPEN' or pr['isDraft'] is not False
                or pr['author']['login'] != repo.author or labels & {'hold', 'changes-requested'}
                or len(labels & {'ready-for-review', 'ready-for-audit'}) != 1
                or not re.fullmatch(r'[0-9a-f]{40,64}', pr['headRefOid'])
                or not re.fullmatch(r'[0-9a-f]{40,64}', pr['baseRefOid'])):
            raise Unverified('multi_claim_candidate_not_published')
        refs = set()
        for issue in pr['closingIssuesReferences']:
            n = issue.get('number')
            if type(n) is not int or n not in covered:
                continue
            identity = (issue.get('repository') or {}).get('nameWithOwner')
            url = issue.get('url')
            if (not identity and not url
                    or identity and identity.lower() != repo.repo.lower()
                    or url and url.lower() != f'https://github.com/{repo.repo}/issues/{n}'.lower()):
                continue
            refs.add(n)
        if refs:
            proofs.append({'pr':number,'head':pr['headRefOid'],'base':pr['baseRefOid'],'issues':sorted(refs)})
            for n in refs:
                covered[n].append(number)
    if any(len(numbers) != 1 for numbers in covered.values()):
        raise Unverified('multi_claim_handoff_incomplete_or_ambiguous')
    # Each claim is published, but the actual checkout must also be published.
    if any(proof['head'] == head for proof in proofs):
        basis = 'pr_head'
    elif any(proof['base'] == head for proof in proofs) and command(git + ['rev-parse', 'origin/main'], run) == head:
        basis = 'published_main'
    else:
        raise Unverified('multi_claim_checkout_not_published')
    if json.loads(snapshot_path(state, repo.repo).read_text()) != snapshot:
        raise Unverified('snapshot_changed')
    if now + time.monotonic() - began - snapshot['observed_at'] > 300:
        raise Unverified('snapshot_expired')
    if command(git + ['status', '--porcelain=v1', '--untracked-files=all'], run):
        raise Unverified('worktree_not_clean')
    if command(git + ['rev-parse', 'HEAD'], run) != head:
        raise Unverified('head_changed')
    if basis == 'published_main' and command(git + ['rev-parse', 'origin/main'], run) != head:
        raise Unverified('published_main_changed')
    identity = session_identity(session, run)
    return {'eligible':True,'reason':'verified_handoff','evidence':{
        'repo':repo.repo,'role':'build','clone':str(clone),'issues':sorted(active),
        'claims':[{'issue':n,'pr':covered[n][0]} for n in sorted(active)],
        'published_prs':proofs,'checkout_basis':basis,'head':head,**identity}}
