"""Explain runtime gates from local receipts without treating them as task outcomes."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def read_blockers(state, repositories, health, activity, now=None):
    """Configured session -> actionable explanation; no remote calls or mutations.

    Retry times mean a cooldown expires, never a promised execution start. The
    last admission receipt is labeled stale after 15 minutes and superseded by
    a later dispatch. Healthy idle sessions are not blockers by themselves.
    """
    now = time.time() if now is None else now
    allowed = {row['short']+'-'+role:(row,role) for row in repositories
               for role in ('build','review','audit')}
    receipts = {}
    pauses = {}
    newer_lifecycle = set()
    path = Path(state)/'activity.sqlite3'
    if path.exists():
        db = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=2)
        try:
            for session,at,event,raw in db.execute("SELECT session,at,event,metadata FROM events WHERE event IN ('dispatch','admission_deferred','paused','session_created') ORDER BY id DESC"):
                if session not in allowed:
                    continue
                metadata = json.loads(raw)
                if event in ('dispatch', 'admission_deferred') and session not in receipts:
                    receipts[session] = (at,event,metadata)
                if event == 'paused' and session not in pauses and session not in newer_lifecycle:
                    pauses[session] = (at,metadata)
                if event in ('dispatch', 'session_created'):
                    newer_lifecycle.add(session)
        finally:
            db.close()
    result = {}
    for session,(row,role) in allowed.items():
        guard = health.get('sessions',{}).get(session,{})
        until = max(guard.get('until',0),health.get('account_until',0))
        reason = (health.get('account_reason','account_cooldown') if health.get('account_until',0)>now
                  else guard.get('reason','cooldown'))
        if until > now:
            retry_at = _iso(until)
            item = dict(kind='worker_cooldown', reason=reason, retry_at=retry_at,
                        remaining_seconds=max(0,int(until-now)), basis='Local supervisor health guard',
                        why=f'Worker retries are paused until {retry_at}.',
                        next='The supervisor can retry after the cooldown, subject to admission limits and the underlying blocker.')
            if reason == 'no_progress':
                worker = {'build': 'Builder', 'review': 'Reviewer', 'audit': 'Auditor'}[role]
                item.update(
                    why=f'{worker} retries are paused until {retry_at}: the supervisor recorded no visible progress. The underlying cause is not recorded in the health guard.',
                    next={
                        'build': 'Open the builder output and inspect its last result and any published PR. Resolve the blocker before resuming; cooldown expiry does not confirm the issue is ready.',
                        'review': 'Open the reviewer output and inspect the latest PR review result. Resolve the blocker before resuming; cooldown expiry does not confirm the review can continue.',
                        'audit': 'Open the auditor output and inspect the latest audit and PR review evidence. A verified human-approval wait requires human review and will not clear when this cooldown expires.',
                    }[role])
                pause = pauses.get(session)
                if pause and pause[1].get('reason') == reason:
                    item['observed_at'] = pause[0]
                    item['evidence'] = {'event': 'paused', 'reason': reason,
                                        'observed_at': pause[0], 'session': session, 'role': role}
                    item['basis'] = 'Local supervisor health guard and matching pause activity receipt'
            elif reason in {'blocked_auth','blocked_quota','blocked_permission'}:
                item['next'] = {'blocked_auth':'Restore the worker account login, then allow the supervisor to recheck.',
                                'blocked_quota':'Restore provider quota or wait for its reset; the local cooldown is not the provider reset time.',
                                'blocked_permission':'Inspect the worker permission failure and correct its environment before retrying.'}[reason]
            result[session] = item
            continue
        receipt = receipts.get(session)
        if not receipt or receipt[1] != 'admission_deferred' or row.get(role,{}).get('up'):
            continue
        at,_,metadata = receipt
        capacity = metadata.get('capacity',{})
        reason = capacity.get('reason',metadata.get('reason','unknown'))
        observed = capacity.get('observed_at')
        age = now-observed if isinstance(observed,(float,int)) else None
        stale = age is None or age > 900 or age < -60
        item = dict(kind='admission_deferred',reason=reason,observed_at=at,stale=stale,
                    basis='Latest admission decision for this session; not current host utilization',
                    why='The last launch attempt was deferred.',
                    next='The supervisor will re-evaluate this queue on its next pass; an execution start time is not guaranteed.')
        limits = capacity.get('limits',{})
        if reason == 'role_session_limit':
            used,limit = capacity.get('role_counts',{}).get(role),limits.get('max_sessions_per_role')
            item['why'] = f'Last launch deferred: {used} {role} sessions occupied the {limit} allowed slots.'
            item['next'] = 'Queued work can launch after a role slot is released and the next admission check passes. Inspect the occupied workers for completed handoffs or unresolved blockers.'
        elif reason == 'fleet_session_limit':
            item['why'] = f"Last launch deferred: {capacity.get('fleet_count')} sessions occupied the {limits.get('max_sessions')} fleet slots."
        elif 'memory' in reason:
            item['why'] = 'The last launch was deferred by the memory admission guard.'
            item['next'] = 'Wait for active work to release memory; compare current memory and pressure before changing capacity.'
        if stale:
            item['why'] += ' This receipt is stale; the current launch gate is unknown.'
        result[session] = item
    return result
