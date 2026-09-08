"""Read-only issue-level projection of local evidence and fresh queue snapshots.

References join PRs to issues; neither a tmux session nor a merged PR proves that
an issue is complete. Worker association is a role-level candidate, not assignment.
"""
from datetime import datetime
import hashlib
import json
import re
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlsplit

from .observability import connect

STEPS = {'backlog': 0, 'ready': 0, 'build': 1, 'review': 2, 'audit': 3, 'done': 4}


def _labels(data):
    return {x.get('name') if isinstance(x, dict) else x for x in data.get('labels', [])}


def _body_references(body, repo):
    """Read explicit closing instructions in inventory text before enrichment.

    Code samples and quoted prose are not implementation links. Qualified
    references retain their repository identity and are validated by the join.
    """
    if not isinstance(body, str):
        return []
    prose = []
    fence = None
    for line in body.splitlines():
        marker = re.match(r'^\s{0,3}(`{3,}|~{3,})', line)
        if marker:
            kind = marker[1][0]
            if fence is None:
                fence = (kind, len(marker[1]))
            elif kind == fence[0] and len(marker[1]) >= fence[1]:
                fence = None
            continue
        if fence or re.match(r'^(?: {4}|\t|\s{0,3}>)', line):
            continue
        prose.append(re.sub(r'(`+).*?\1', '', line))
    text = '\n'.join(prose)
    pattern = (r'(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+'
               r'(?:(https://github\.com/[\w.-]+/[\w.-]+/issues/[1-9][0-9]*)'
               r'|(?:([\w.-]+/[\w.-]+))?#([1-9][0-9]*))\b')
    refs = []
    for match in re.finditer(pattern, text):
        url, qualified, number = match.groups()
        if url:
            parts = urlsplit(url).path.strip('/').split('/')
            refs.append({'repo': '/'.join(parts[:2]), 'number': int(parts[3]),
                         'url': url, 'source': 'closing_keyword_inventory'})
        else:
            refs.append({'repo': qualified or repo, 'number': int(number),
                         'source': 'closing_keyword_inventory'})
    return refs


def _references(pr, repo):
    refs = list(pr.get('linked_issues') or [])
    if isinstance(pr.get('body'), str):
        # Inventory body is the current version. Do not retain an old textual
        # link after its closing instruction was removed or changed.
        refs = [ref for ref in refs if not isinstance(ref, dict)
                or ref.get('source') not in ('closing_keyword', 'closing_keyword_inventory')]
        refs.extend(_body_references(pr['body'], repo))
    canonical = (pr.get('raw_pr_summary') or {}).get('closingIssuesReferences', []) or []
    if isinstance(canonical, dict):
        canonical = canonical.get('nodes', [])
    refs.extend({**ref, 'source': 'closingIssuesReferences'} for ref in canonical if isinstance(ref, dict))
    result = {}
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        number = ref.get('number')
        if type(number) is not int or number <= 0:
            continue
        identities = []
        if ref.get('repo'):
            identities.append(ref['repo'])
        repository = ref.get('repository')
        if isinstance(repository, dict):
            identity = repository.get('nameWithOwner')
            if not identity and repository.get('name') and isinstance(repository.get('owner'), dict):
                owner = repository['owner'].get('login')
                identity = f"{owner}/{repository['name']}" if owner else None
            if identity:
                identities.append(identity)
        elif repository:
            identities.append(repository)
        if ref.get('url'):
            try:
                parsed = urlsplit(ref['url'])
                parts = parsed.path.strip('/').split('/')
                if parsed.hostname != 'github.com' or len(parts) != 4 or parts[2] != 'issues' or parts[3] != str(number):
                    continue
                identities.append('/'.join(parts[:2]))
            except (ValueError, TypeError, AttributeError):
                continue
        # Canonical closing references can target another repository. Never
        # assume the PR repository when reference identity is missing/ambiguous.
        if not identities or any(not isinstance(identity, str) or identity.lower() != repo.lower() for identity in identities):
            continue
        result[number] = ref.get('source', 'explicit_reference')
    return result


def _absent_from_fresh_queue(item, row):
    """Absence is useful only in a complete snapshot newer than local evidence."""
    keys = ('ready', 'wip', 'review', 'audit', 'sentback', 'limbo', 'held', 'conflicts')
    work = row.get('work') or {}
    if (row.get('queue_status') != 'ok' or item.get('state') == 'closed'
            or any(not isinstance(work.get(key), list) for key in keys)):
        return False
    observed = _epoch(row.get('observed_at'))
    if not observed or observed < _epoch(item.get('observed_at')):
        return False
    return not any(item['number'] in work[key] for key in keys)


def _pr_stage(pr, row):
    number = pr['number']
    if row.get('queue_status') == 'ok':
        work = row.get('work') or {}
        if number in work.get('held', []):
            return 'held'
        if number in work.get('conflicts', []):
            return 'unknown'
        for key, stage in [('sentback', 'build'), ('limbo', 'build'), ('review', 'review'), ('audit', 'audit')]:
            if number in work.get(key, []):
                return stage
    if pr.get('state') == 'closed':
        return 'merged' if pr.get('merged_at') else 'closed'
    if pr.get('stage') in ('build', 'review', 'audit') and _absent_from_fresh_queue(pr, row):
        return 'unknown'
    return {'completed': 'merged'}.get(pr.get('stage'), pr.get('stage', 'unknown'))


def _approval_action(pr, repo):
    """Expose a recorded current-candidate audit wait, never grant approval."""
    head = pr.get('head_sha')
    if (pr.get('state') != 'open' or pr.get('review_decision') not in ('REVIEW_REQUIRED', 'UNKNOWN', '', None)
            or pr.get('summary_status') != 'ok' or not head
            or pr.get('summary_head_sha') != head
            or 'critical' not in {str(label).lower() for label in _labels(pr)}):
        return None
    audits = [e for e in pr.get('evidence', []) if e.get('kind') == 'audit']
    if not audits:
        return None
    audit = max(audits, key=lambda e: e.get('created_at') or '')
    body = audit.get('body') or ''
    # A critical label or aggregate GitHub review requirement alone says nothing
    # about audit completion. Require the auditor's explicit PASS and waiting
    # record bound to the collected candidate; later audit records supersede it.
    if not (re.search(r'(?im)^\s*(?:#{1,6}\s*)?(?:\*{1,2})?(?:security audit|auditor|audit)\b[^\n]*\bPASS\b', body)
            and re.search(r'(?i)criticality:\s*CRITICAL\b', body)
            and re.search(r'(?i)awaiting human approval', body)):
        return None
    if audit.get('commit_id'):
        if audit['commit_id'] != head:
            return None
    elif not re.search(r'(?i)\bhead\b[^\n]{0,60}' + re.escape(head) + r'\b', body):
        return None
    if pr.get('base_sha') and pr['base_sha'] not in body:
        return None
    return {'kind': 'human_approval',
            'why': f'PR #{pr["number"]}: security audit recorded PASS; critical work is awaiting human approval',
            'impact': 'This issue cannot complete until qualifying human reviews and repository protections are satisfied. Forge cannot bypass approval for critical work.',
            'next': 'Review the audit and current PR changes. An authorized human reviewer other than the author must approve or request changes.',
            'link': f'https://github.com/{repo}/pull/{pr["number"]}',
            'evidence_url': audit.get('url'), 'head_sha': head,
            'observed_at': pr.get('summary_observed_at'),
            'basis': ('Recorded current-candidate audit and GitHub review requirement'
                      if pr.get('review_decision') == 'REVIEW_REQUIRED'
                      else 'Recorded current-candidate audit and Forge critical-work human approval policy; GitHub aggregate review requirement unavailable')
                     + '; not independent approval verification'}


def _stalled_queue(state, row, health, now):
    """Bind a current watchdog pause to its recorded queue, not future arrivals."""
    if row.get('queue_status') != 'ok' or not row.get('audit', {}).get('up'):
        return None
    session = f"{row.get('short', row['repo'].split('/')[-1])}-audit"
    guard = (health or {}).get('sessions', {}).get(session, {})
    until = guard.get('until')
    if (guard.get('reason') != 'no_progress' or type(until) not in (int, float)
            or not now < until):
        return None
    queue = set((row.get('work') or {}).get('audit', []))
    if not queue:
        return None
    path = Path(state).absolute() / 'activity.sqlite3'
    db = None
    try:
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
        pause = db.execute("SELECT at,metadata FROM events WHERE session=? AND event='paused' ORDER BY at DESC,id DESC LIMIT 1", (session,)).fetchone()
        if not pause or json.loads(pause[1]).get('reason') != 'no_progress':
            return None
        stamp = pause[0]
        began = datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp()
        if not began <= now < until:
            return None
        if db.execute("SELECT 1 FROM events WHERE session=? AND event='session_created' AND at>? LIMIT 1", (session, stamp)).fetchone():
            return None
        event = db.execute("SELECT at,metadata FROM events WHERE session=? AND repo=? AND role='audit' AND event='pipeline_observed' AND at<=? ORDER BY at DESC,id DESC LIMIT 1", (session, row['repo'], stamp)).fetchone()
        if not event:
            return None
        meta = json.loads(event[1])
        observed = datetime.fromisoformat(event[0].replace('Z', '+00:00')).timestamp()
        if (not 0 <= began - observed <= 900 or meta.get('snapshot_status') != 'ok'
                or set(meta.get('queued', [])) != queue):
            return None
        return {'session': session, 'until': until, 'observed_at': event[0], 'queue': queue}
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return None
    finally:
        if db:
            db.close()


def _epoch(value):
    if type(value) in (int, float):
        return value
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, AttributeError):
        return 0


def _verified_wait_receipts(state, rows, items, now):
    """Consume fresh check results; failed checks invalidate older UI evidence."""
    verified, checked = {}, set()
    for repo, row in rows.items():
        path = Path(state) / 'human-wait-checks' / (hashlib.sha256(repo.lower().encode()).hexdigest() + '.json')
        try:
            receipt = json.loads(path.read_text())
            at = _epoch(receipt.get('observed_at'))
            if receipt.get('repo') != repo or not 0 <= now - at <= 300:
                continue
            checked.add(repo)
            if (receipt.get('eligible') is not True or receipt.get('reason') != 'verified_awaiting_human_approval'
                    or row.get('queue_status') != 'ok' or not 0 <= now - _epoch(row.get('observed_at')) <= 300):
                continue
            evidence = receipt.get('evidence') or {}
            candidates = evidence.get('candidates') or []
            queue = set((row.get('work') or {}).get('audit', []))
            if evidence.get('repo') != repo or not queue or {c.get('pr') for c in candidates} != queue:
                continue
            accepted = {}
            for candidate in candidates:
                number = candidate.get('pr')
                issue_numbers = candidate.get('issues') or []
                if (type(number) is not int or not issue_numbers
                        or any(type(n) is not int or n <= 0 for n in issue_numbers)
                        or any(not re.fullmatch(r'[0-9a-f]{40,64}', candidate.get(k) or '') for k in ('head', 'base'))):
                    break
                detail = (row.get('item_details') or {}).get(str(number))
                if not isinstance(detail, dict):
                    break
                labels = {str(label).lower() for label in _labels(detail)}
                if (detail.get('state') != 'open' or detail.get('kind') != 'pr'
                        or not {'critical', 'ready-for-audit'} <= labels
                        or labels & {'hold', 'changes-requested', 'ready-for-review'}):
                    break
                current = items.get((repo, number), {})
                if _epoch(current.get('observed_at')) > at:
                    if (current.get('state') == 'closed'
                            or any(current.get(field) and current[field] != candidate[key]
                                   for field, key in (('head_sha', 'head'), ('base_sha', 'base')))):
                        break
                refs = _references(detail, repo)
                if _epoch(row.get('observed_at')) >= at and refs and set(refs) != set(issue_numbers):
                    break
                if any(items.get((repo, n), {}).get('kind') == 'pr'
                       or (items.get((repo, n), {}).get('state') == 'closed'
                           and _epoch(items[(repo, n)].get('observed_at')) > at) for n in issue_numbers):
                    break
                audit_url = candidate.get('audit_url') or ''
                parsed = urlsplit(audit_url)
                if parsed.scheme != 'https' or parsed.hostname != 'github.com' or parsed.path.lower() != f'/{repo}/pull/{number}'.lower():
                    break
                accepted[(repo, number)] = {**candidate, 'observed_at': at}
            else:
                verified.update(accepted)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return verified, checked


def _receipt_action(repo, candidate):
    return {'kind': 'human_approval',
            'why': f'PR #{candidate["pr"]}: security audit recorded PASS; critical work is awaiting human approval',
            'impact': 'Forge cannot complete this critical issue until required human reviews are satisfied.',
            'next': 'Review the audit and current PR changes. An authorized human reviewer other than the author must approve or request changes.',
            'link': f'https://github.com/{repo}/pull/{candidate["pr"]}',
            'evidence_url': candidate['audit_url'], 'head_sha': candidate['head'],
            'observed_at': candidate['observed_at'],
            'basis': 'Fresh local human-wait check verified current GitHub head, base, authored audit, and reviews; Forge critical-work approval policy'}


def read_flow(state, repository_rows, health=None, *, now=None):
    """Return unique issues; main list excludes backlog and historical completion."""
    rows = {row['repo']: row for row in repository_rows}
    observed_now = time.time() if now is None else now
    stalled = {repo: _stalled_queue(state, row, health, observed_now) for repo, row in rows.items()}
    items = {}
    status = 'unavailable'
    db = None
    try:
        db = connect(Path(state).absolute())
        if db:
            for stored in db.execute('SELECT * FROM items'):
                if stored['repo'] not in rows:
                    continue
                data = json.loads(stored['data'])
                items[(stored['repo'], stored['number'])] = {
                    **data, 'repo': stored['repo'], 'number': stored['number'],
                    'kind': stored['kind'], 'stage': stored['stage'],
                    'updated_at': stored['updated_at'], 'observed_at': stored['observed_at']}
            status = 'ok'
    except (OSError, sqlite3.Error, ValueError, TypeError):
        status = 'unavailable'
    finally:
        if db:
            db.close()
    # The scheduler already reads GitHub's open inventory. Its fresh minimal
    # metadata closes the interval before the slower history collector sees a
    # new PR, without adding network calls or inventing links from issue claims.
    for repo, row in rows.items():
        if row.get('queue_status') != 'ok':
            continue
        details = row.get('item_details') or {}
        if not isinstance(details, dict):
            continue
        for key, entry in details.items():
            if not isinstance(entry, dict):
                continue
            number = entry.get('number')
            if (type(number) is not int or number <= 0 or str(number) != str(key)
                    or entry.get('kind') not in ('issue', 'pr') or entry.get('state') != 'open'
                    or entry.get('repo', repo) != repo or (repo, number) in items):
                continue
            labels = entry.get('labels') if isinstance(entry.get('labels'), list) else []
            body = entry.get('body') if isinstance(entry.get('body'), str) else ''
            items[(repo, number)] = {'repo': repo, 'number': number, 'kind': entry['kind'],
                'title': entry.get('title'), 'body': body, 'labels': labels, 'state': 'open',
                'stage': 'unknown' if entry['kind'] == 'pr' else 'pending',
                'url': entry.get('url') or entry.get('html_url'),
                'created_at': entry.get('created_at'), 'updated_at': entry.get('updated_at'),
                'observed_at': row.get('observed_at'), 'metadata_source': 'snapshot_inventory'}
    # Issue identity is explicit in ready/wip and scheduling issue lists. Held
    # snapshots mix issue and PR identities, so unknown held numbers stay unknown.
    for repo, row in rows.items():
        if row.get('queue_status') != 'ok':
            continue
        work, schedule = row.get('work') or {}, row.get('schedule') or {}
        numbers = set(work.get('ready', []) + work.get('wip', []) + schedule.get('active', []) + schedule.get('eligible', []))
        for number in numbers:
            items.setdefault((repo, number), {'repo': repo, 'number': number, 'kind': 'issue', 'title': f'Issue #{number}'})
    verified_waits, checked_waits = _verified_wait_receipts(state, rows, items, observed_now)
    links, unlinked = {}, []
    for (repo, number), pr in list(items.items()):
        if pr.get('kind') != 'pr':
            continue
        receipt = verified_waits.get((repo, number))
        refs = ({n: 'verified_human_wait_canonical_reference' for n in receipt['issues']}
                if receipt else _references(pr, repo))
        for issue_number, basis in refs.items():
            key = (repo, issue_number)
            if items.get(key, {}).get('kind') == 'pr':
                continue
            items.setdefault(key, {'repo': repo, 'number': issue_number, 'kind': 'issue', 'title': f'Issue #{issue_number}'})
            links.setdefault(key, []).append((pr, basis))
        if not refs and _pr_stage(pr, rows[repo]) not in ('closed', 'merged'):
            unlinked.append({'repo': repo, 'number': number, 'title': pr.get('title'),
                             'stage': _pr_stage(pr, rows[repo]), 'reason': 'No explicit same-repository issue reference in local evidence'})
    issues = []
    for (repo, number), data in items.items():
        if data.get('kind') != 'issue':
            continue
        row = rows[repo]
        fresh = row.get('queue_status') == 'ok'
        work = row.get('work') or {} if fresh else {}
        schedule = row.get('schedule') or {} if fresh else {}
        labels = _labels(data)
        prs = links.get((repo, number), [])
        blockers = []
        actions = []
        basis = (['fresh snapshot inventory'] if data.get('metadata_source') == 'snapshot_inventory'
                 else ['local issue evidence'] if data.get('observed_at')
                 else ['fresh snapshot issue identity' if fresh else 'explicit PR reference; issue metadata unavailable'])
        state_name = 'backlog'
        if 'ready' in labels:
            state_name = 'ready'
        if 'wip' in labels:
            state_name = 'build'
        if number in work.get('ready', []) or number in schedule.get('eligible', []):
            state_name = 'ready'
            basis.append('fresh issue queue')
        if number in work.get('wip', []) or number in schedule.get('active', []):
            state_name = 'build'
            basis.append('fresh issue claim; not proof of active execution')
        pr_stages = [_pr_stage(pr, row) for pr, _ in prs]
        missing_prs = [pr['number'] for pr, _ in prs
                       if pr.get('stage') in ('build', 'review', 'audit') and _absent_from_fresh_queue(pr, row)]
        reconciliation_pending = bool(missing_prs)
        reconciliation_reason = ('Previously active PR ' + ', '.join('#' + str(n) for n in missing_prs)
                                 + ' is no longer in the current queue; awaiting local status verification.'
                                 if missing_prs else None)
        unfinished = [s for s in pr_stages if s in ('build', 'review', 'audit')]
        if unfinished:
            state_name = min(unfinished, key=STEPS.get)
        if len(set(unfinished)) > 1:
            blockers.append('Linked PRs are at different stages; showing earliest unfinished step')
        if 'unknown' in pr_stages:
            conflicting = any(stage == 'unknown' and pr['number'] not in missing_prs
                              for (pr, _), stage in zip(prs, pr_stages))
            if conflicting or not unfinished:
                state_name = 'unknown'
            if conflicting:
                blockers.append('Linked PR stage is unknown or has conflicting labels')
        if prs and not unfinished and all(s in ('merged', 'closed') for s in pr_stages):
            state_name = 'unknown'
            blockers.append('Linked PRs are closed; issue completion is not confirmed')
        if not prs and state_name in ('ready', 'build') and _absent_from_fresh_queue(data, row):
            state_name = 'unknown'
            reconciliation_pending = True
            reconciliation_reason = 'Previously queued issue is no longer in the current queue; awaiting local status verification.'
        held = 'hold' in labels or number in work.get('held', []) or 'held' in pr_stages
        if held:
            blockers.append('Issue or linked PR has a hold label; remove the hold when safe to resume')
        for pr, link_basis in prs:
            basis.append(f'PR #{pr["number"]}: {link_basis}')
            if pr.get('metadata_source') == 'snapshot_inventory':
                basis.append(f'PR #{pr["number"]}: fresh snapshot inventory')
            wait = verified_waits.get((repo, pr['number']))
            action = (_receipt_action(repo, wait) if wait
                      else None if repo in checked_waits or pr['number'] in missing_prs else _approval_action(pr, repo))
            if action:
                actions.append(action)
                blockers.append(f'PR #{pr["number"]} awaits human approval after recorded security audit PASS')
                held = True
            if not action and pr['number'] not in missing_prs and pr.get('state') == 'open' and pr.get('review_decision') == 'REVIEW_REQUIRED':
                blockers.append(f'PR #{pr["number"]} requires a review')
            if pr['number'] not in missing_prs and pr.get('state') == 'open' and (pr.get('human_approval_required') or 'human-approval-required' in _labels(pr)):
                blockers.append(f'PR #{pr["number"]} requires human approval')
                held = True
        reasons = schedule.get('blocked', {}).get(str(number), [])
        if isinstance(reasons, str):
            reasons = [reasons]
        # Admission exclusions are not execution failures: an existing claim
        # must not be counted as a blocker merely because it cannot start twice.
        reasons = [reason for reason in reasons if reason != 'active_claim'
                   and not (reason == 'not_ready' and state_name in ('build', 'review', 'audit'))]
        blockers.extend(str(reason).replace('_', ' ') for reason in reasons)
        step = STEPS.get(state_name)
        if held or (reasons and state_name in ('ready', 'backlog')):
            state_name = 'held'
        if data.get('state') == 'closed':
            state_name = 'done' if data.get('state_reason') == 'completed' else 'unknown'
            step = STEPS.get(state_name)
            blockers = [] if state_name == 'done' else ['Issue closed without completed state reason']
            actions = []
            reconciliation_pending = False
            reconciliation_reason = None
        attention_state = 'CRITICAL' if any(action['kind'] == 'human_approval' for action in actions) else None
        stall = stalled.get(repo)
        if state_name == 'audit' and not attention_state and stall:
            audit_prs = [pr for pr, _ in prs if pr['number'] in stall['queue']]
            references = {n for pr in audit_prs for n in _references(pr, repo)}
            if {pr['number'] for pr in audit_prs} == stall['queue'] and references == {number}:
                attention_state = 'STALLED'
                message = 'Security auditor stopped making progress on this issue; automatic retries are paused'
                blockers.append(message)
                actions.append({'kind': 'stalled_audit', 'why': message,
                    'impact': 'This issue is still at security audit and needs inspection before work can resume.',
                    'next': 'Inspect the auditor output and latest PR evidence, resolve the reported blocker, then resume auditing.',
                    'link': f'https://github.com/{repo}/pull/{audit_prs[0]["number"]}',
                    'session': stall['session'], 'role': 'audit', 'observed_at': stall['observed_at'],
                    'cooldown_until': stall['until'],
                    'basis': 'Current no-progress cooldown bound to the same locally recorded audit queue'})
        role = {1: 'build', 2: 'review', 3: 'audit'}.get(step)
        worker = row.get(role, {}) if role else {}
        workers = []
        if fresh and worker.get('up') and not attention_state and state_name not in ('held', 'done', 'unknown'):
            workers = [{'session': f'{row.get("short", repo.split("/")[-1])}-{role}', 'role': role,
                        'active': bool(worker.get('busy')), 'basis': 'repository role candidate; exact issue assignment unverified'}]
        issues.append({'repo': repo, 'number': number, 'kind': 'issue',
                       'url': data.get('url'), 'created_at': data.get('created_at'),
                       'closed_at': data.get('closed_at'),
                       'state': state_name, 'attention_state': attention_state,
                       'reconciliation_pending': reconciliation_pending, 'reconciliation_reason': reconciliation_reason, 'on_hold': bool(held), 'step': step, 'total_steps': 4,
                       'actions': actions, 'pr_numbers': sorted(pr['number'] for pr, _ in prs), 'blockers': list(dict.fromkeys(blockers)),
                       'updated_at': data.get('updated_at'), 'observed_at': data.get('observed_at'),
                       'snapshot_status': row.get('queue_status', 'unknown'), 'workers': workers,
                       'basis': basis, 'title': data.get('title') or f'Issue #{number}'})
    issues.sort(key=lambda x: (not any(w['active'] for w in x['workers']), x['repo'], x['number']))
    return {'status': status, 'issues': [x for x in issues if x['state'] not in ('backlog', 'done')],
            'backlog': [x for x in issues if x['state'] == 'backlog'],
            'completed': [x for x in issues if x['state'] == 'done'],
            'counts': {'in_flight': sum(x['state'] in ('build', 'review', 'audit') for x in issues),
                       'ready': sum(x['state'] == 'ready' for x in issues),
                       'blocked': sum(bool(x['blockers']) for x in issues),
                       'done': sum(x['state'] == 'done' for x in issues)},
            'unlinked_prs': unlinked}
