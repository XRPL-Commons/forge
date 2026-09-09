"""Durable bridge from verified process release to the local execution ledger.

A prepared intent never establishes a successful kill. Only confirmed receipts
are replayed. Issue completion is never inferred from worker retirement.
"""
import hashlib
import json
from pathlib import Path
import stat
import time

from .control import atomic_json


def _engine():
    from . import workflow
    return workflow


def _initialized(state):
    root = Path(state)
    return (root / 'workflow.sqlite3').exists() or (root / 'workflow.initialized').exists()


def _generation(evidence):
    from .codex_worker import parse_generation
    raw = '|'.join(str(evidence[k]) for k in ('session_id', 'session_created', 'pane_id', 'pane_pid'))
    return parse_generation(raw)['id']


def _directory(state):
    directory = Path(state) / 'workflow-releases'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise ValueError('workflow_release_directory_not_private')
    return directory


def _write(path, value):
    atomic_json(path, value)


def _slot(state, session):
    view = _engine().read_state(state)
    if view.get('status') != 'ok':
        raise ValueError('workflow_release_read_unavailable')
    matches = [s for s in view['slots'] if s.get('session') == session]
    if len(matches) > 1:
        raise ValueError('workflow_release_slot_ambiguous')
    return matches[0] if matches else None


def prepare(state, session, proof, *, action='retired', unbound_startup=False):
    """Persist exact release intent before kill; raise on initialized-ledger errors."""
    if not _initialized(state):
        return None
    try:
        if action not in {'retired', 'migrate'}:
            raise ValueError('workflow_release_action_invalid')
        evidence = proof['evidence']
        generation = _generation(evidence)
        slot = _slot(state, session)
        if slot and slot.get('generation') not in (None, generation):
            raise ValueError('workflow_release_generation_mismatch')
        if slot and (slot.get('repo') != evidence['repo'].lower() or slot.get('role') != evidence['role']):
            raise ValueError('workflow_release_repository_mismatch')
        assignment = slot.get('assignment_id') if slot else None
        cancelling_unbound = False
        if assignment and slot.get('generation') != generation:
            view = _engine().read_state(state)
            matches = [a for a in view['assignments'] if a['id'] == assignment]
            a = matches[0] if len(matches) == 1 else {}
            if (unbound_startup is not True or action != 'retired'
                    or proof.get('reason') != 'verified_interrupted_startup'
                    or type(evidence.get('creation_event')) is not int or evidence['creation_event'] <= 0
                    or a.get('state') not in {'reserved', 'blocked'} or a.get('generation') is not None
                    or a.get('submitted_at') is not None
                    or a.get('session') != session or a.get('repo') != evidence['repo'].lower()
                    or a.get('role') != evidence['role']
                    or type(a.get('created_at')) not in (int, float)
                    or a['created_at'] > evidence['session_created'] + 1):
                raise ValueError('workflow_release_assignment_unbound')
            # This cancels an undispatched reservation after its verified empty
            # startup shell is killed; it never binds that shell retroactively.
            cancelling_unbound = True
        key = hashlib.sha256((session + '\0' + generation + '\0' + action).encode()).hexdigest()
        value = {'schema': 1, 'id': key, 'session': session, 'generation': generation,
                 'assignment_id': assignment, 'repo': evidence['repo'].lower(), 'role': evidence['role'],
                 'provider': slot.get('provider') if slot else evidence.get('provider'),
                 'slot_generation': slot.get('generation') if slot else None,
                 'unbound_startup': cancelling_unbound,
                 'action': action, 'reason': proof['reason'], 'phase': 'prepared', 'prepared_at': time.time()}
        from .workflow_results import from_handoff
        result=from_handoff(evidence) if action=='retired' else None
        if result is not None:value['issue_result']=result
        directory = _directory(state)
        prepared = directory / 'prepared'
        prepared.mkdir(mode=0o700, exist_ok=True)
        path = prepared / (key + '.json')
        if path.exists():
            previous = json.loads(path.read_text())
            if previous.get('phase') != 'prepared':
                raise ValueError('workflow_release_already_confirmed')
        _write(path, value)
        return value
    except Exception as exc:
        if isinstance(exc, ValueError) and str(exc).startswith('workflow_'):
            raise
        raise ValueError('workflow_release_prepare_unavailable') from exc


def _apply(state, ticket):
    engine = _engine()
    generation = None if ticket.get('unbound_startup') else ticket['generation']
    proof = {'kind': 'verified_no_dispatch' if ticket.get('unbound_startup') else
             'verified_provider_handover' if ticket['action'] == 'migrate' else 'verified_release',
             'verified': True, 'released': True, 'assignment_id': ticket['assignment_id'],
             'generation': generation, 'released_generation': ticket['generation'],
             'session': ticket['session'], 'reason': ticket['reason']}
    if ticket.get('issue_result') is not None:
        from .workflow_results import record
        record(state,ticket['issue_result'])
    if ticket['assignment_id']:
        result = engine.transition(state, ticket['assignment_id'], ticket['action'],
            generation=generation, reason=ticket['reason'], proof=proof,
            source_event_id='release:' + ticket['id'])
        if isinstance(result, dict) and result.get('ok') is False:
            raise ValueError('workflow_release_transition_rejected')
    else:
        # The engine checks exact generation and rejects bound replacement slots;
        # its event replay runs first, allowing an applied receipt to be archived.
        engine.observe_slot(state, ticket['session'], ticket['repo'], ticket['role'],
                            slot_state='free', generation=ticket['slot_generation'],
                            proof=proof, source_event_id='release:' + ticket['id'])


def _archive(path, value):
    directory = path.parent / 'applied'
    directory.mkdir(mode=0o700, exist_ok=True)
    _write(directory / path.name, {**value, 'phase': 'applied', 'applied_at': time.time()})
    path.unlink(missing_ok=True)
    (path.parent / 'prepared' / path.name).unlink(missing_ok=True)


def confirmed(state, ticket):
    """Call only after successful exact-session kill. Never change that outcome."""
    if ticket is None:
        return {'status': 'legacy'}
    value = {**ticket, 'phase': 'confirmed', 'confirmed_at': time.time()}
    path = Path(state) / 'workflow-releases' / (ticket['id'] + '.json')
    try:
        _write(path, value)
        _apply(state, value)
        _archive(path, value)
        return {'status': 'applied', 'event_id': ticket['id']}
    except Exception:
        # Keep confirmed durable intent for reconciliation. If even recording
        # success fails, prepared intent remains uncertain and cannot auto-free.
        return {'status': 'pending', 'event_id': ticket['id'], 'reason': 'workflow_release_reconciliation_required'}


def reconcile(state, *, limit=20):
    """Replay bounded confirmed receipts only; caller holds supervisor lock."""
    directory = Path(state) / 'workflow-releases'
    results = []
    if not directory.exists():
        return results
    for path in sorted(directory.glob('*.json'))[:max(0, min(limit, 100))]:
        try:
            if path.is_symlink() or path.stat().st_size > 8192:
                continue
            value = json.loads(path.read_text())
            if value.get('phase') != 'confirmed' or path.name != value.get('id', '') + '.json':
                continue
            _apply(state, value)
            _archive(path, value)
            results.append({'event_id': value['id'], 'status': 'applied'})
        except Exception:
            results.append({'event_id': path.stem, 'status': 'pending'})
    return results
