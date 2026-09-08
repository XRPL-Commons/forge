"""Refund only launch charges for verified successful worker generations.

The hourly budget protects against failed starts and crashes. Productive work
that ends through a verified handoff must not consume that failure budget.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import math
from pathlib import Path
import sqlite3
import time

from .config import load_repositories
from .control import atomic_json

SUCCESS = frozenset({'verified_handoff', 'verified_build_complete', 'verified_review_complete',
                     'verified_audit_complete', 'verified_awaiting_human_approval'})


def _epoch(value):
    if type(value) in (int, float):
        return float(value) if math.isfinite(value) else None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, AttributeError, TypeError):
        return None


def _receipt(state, session, stop):
    for folder in ('handoffs', 'human-waits'):
        try:
            value = json.loads((Path(state) / folder / (session + '.json')).read_text())
            at = _epoch(value.get('observed_at'))
            if (value.get('released') is True and value.get('reason') == stop['metadata'].get('reason')
                    and at is not None and abs(at - stop['epoch']) <= 2):
                return value
        except (OSError, ValueError, TypeError):
            pass
    return None


def _plan(config, state, guard, *, session=None, proof=None, now):
    allowed = {r.short + '-' + role: (r.repo, role) for r in load_repositories(config) if r.enabled
               for role in ('build', 'review', 'audit')}
    if session:
        allowed = {session: allowed[session]} if session in allowed else {}
    path = Path(state).absolute() / 'activity.sqlite3'
    if not path.exists():
        return []
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
    since = datetime.fromtimestamp(now - 3605, timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    generations, proposals = {}, []
    try:
        events = db.execute("SELECT id,at,event,session,repo,role,metadata FROM events WHERE at>=? AND event IN ('session_created','dispatch','activity_changed','session_stopped') ORDER BY id", (since,))
        for event_id, at, event, name, repo, role, raw in events:
            if name not in allowed or (repo, role) != allowed[name]:
                continue
            observed = _epoch(at)
            if observed is None or observed > now + 1:
                continue
            metadata = json.loads(raw)
            entry = {'id': event_id, 'epoch': observed, 'at': at, 'metadata': metadata}
            if event == 'session_created':
                generations[name] = {'created': entry, 'dispatch': None, 'starts': set(), 'invalid': False}
                continue
            generation = generations.get(name)
            if not generation:
                continue
            if event == 'activity_changed' and metadata.get('session_started_at'):
                started = _epoch(metadata['session_started_at'])
                if started is None:
                    generation['invalid'] = True
                else:
                    generation['starts'].add(started)
            elif event == 'dispatch':
                if metadata.get('run_id') and generation['dispatch'] is None:
                    generation['dispatch'] = entry
            elif event == 'session_stopped':
                generations.pop(name)
                reason = metadata.get('reason')
                expected_role = {'verified_build_complete': 'build', 'verified_review_complete': 'review',
                                 'verified_audit_complete': 'audit', 'verified_awaiting_human_approval': 'audit'}.get(reason)
                if (reason not in SUCCESS or not generation['dispatch'] or generation['invalid']
                        or expected_role is not None and expected_role != role):
                    continue
                receipt = proof if proof and session == name else _receipt(state, name, entry)
                if receipt:
                    evidence = receipt.get('evidence') or {}
                    released_at = _epoch(receipt.get('observed_at'))
                    created = _epoch(evidence.get('session_created'))
                    if (receipt.get('released') is not True or receipt.get('reason') != metadata['reason']
                            or evidence.get('repo') != repo or evidence.get('role') != role
                            or released_at is None or abs(released_at - observed) > 2 or created is None):
                        continue
                    generation['starts'].add(created)
                elif proof:
                    continue
                if len(generation['starts']) != 1:
                    continue
                created = next(iter(generation['starts']))
                creation = generation['created']
                dispatched = generation['dispatch']
                if not (0 <= creation['epoch'] - created <= 2
                        and creation['id'] < dispatched['id'] < event_id
                        and created <= dispatched['epoch'] <= observed):
                    continue
                record = guard.get('sessions', {}).get(name, {})
                charges = [charge for charge in record.get('launches', [])
                           if type(charge) in (int, float) and math.isfinite(charge)
                           and now - 3600 < charge <= now
                           and created - 5 <= charge < created + 1]
                if len(charges) != 1:
                    continue
                refund_id = str(event_id)
                if refund_id in record.get('successful_launch_refunds', {}):
                    continue
                proposals.append({'session': name, 'repo': repo, 'role': role,
                    'launch_at': charges[0], 'session_created': created,
                    'session_id': (receipt or {}).get('evidence', {}).get('session_id'),
                    'creation_event_id': creation['id'], 'dispatch_event_id': dispatched['id'],
                    'stop_event_id': event_id, 'completed_at': at, 'reason': metadata['reason'],
                    'basis': 'Unique launch charge matched to observed generation, dispatch, and verified successful stop'})
    finally:
        db.close()
    # Ambiguous timing can never refund the same charge for two generations.
    return [p for p in proposals if sum(q['session'] == p['session'] and q['launch_at'] == p['launch_at'] for q in proposals) == 1]


def plan(config, state, *, session=None, proof=None, now=None):
    """Read-only reviewable refund proposal, including historical successes."""
    now = time.time() if now is None else now
    try:
        guard = json.loads((Path(state) / 'legacy-health/guard.json').read_text())
        refunds = _plan(config, state, guard, session=session, proof=proof, now=now)
        return {'status': 'ok', 'refunds': refunds}
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return {'status': 'unavailable', 'refunds': []}


def refund(config, state, *, session=None, proof=None, lock_fd=8, now=None):
    """Caller holds supervisor lock; guard and refund receipts change atomically."""
    from .human_wait import _locked
    if not _locked(state, lock_fd):
        return {'status': 'supervisor_lock_required', 'refunds': []}
    now = time.time() if now is None else now
    directory = Path(state) / 'legacy-health'
    try:
        with (directory / 'guard.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not _locked(state, lock_fd):
                return {'status': 'supervisor_lock_required', 'refunds': []}
            path = directory / 'guard.json'
            guard = json.loads(path.read_text())
            refunds = _plan(config, state, guard, session=session, proof=proof, now=now)
            for value in refunds:
                record = guard['sessions'][value['session']]
                record['launches'].remove(value['launch_at'])
                record.setdefault('successful_launch_refunds', {})[str(value['stop_event_id'])] = {**value, 'refunded_at': now}
                recent = [at for at in record['launches'] if at > now - 3600]
                if record.get('reason') == 'launch_budget_exhausted' and len(recent) < 3:
                    record['until'] = 0
                    record.pop('reason', None)
            if refunds:
                atomic_json(path, guard)
            return {'status': 'ok', 'refunds': refunds}
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return {'status': 'unavailable', 'refunds': []}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'refund'))
    parser.add_argument('--config', required=True)
    parser.add_argument('--state', required=True)
    parser.add_argument('--session')
    parser.add_argument('--lock-fd', type=int, default=8)
    args = parser.parse_args()
    kwargs = {'session': args.session}
    if args.action == 'refund':
        kwargs['lock_fd'] = args.lock_fd
    result = (refund if args.action == 'refund' else plan)(args.config, args.state, **kwargs)
    print(json.dumps(result, sort_keys=True))
    return 0 if result['status'] == 'ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
