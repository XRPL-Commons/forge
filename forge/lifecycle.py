"""Durable worker decisions: explain retained slots without guessing task outcomes.

These records are observations, not authority to kill a worker or change a PR.
Only a current, repeatedly retained, idle generation becomes operator attention.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import time

from .config import load_repositories
from .control import atomic_json, snapshot_path

PHASES = {'cleanup', 'human_wait_cleanup', 'startup_cleanup', 'provider_handover', 'codex_resume'}
QUIET_REASONS = {'role_has_work', 'multi_claim_role_has_work', 'no_tmux_server'}
LONG_WAIT_REASONS = {'not_explicitly_idle', 'processes_not_quiescent'}
EXPLANATIONS = {
    'detached_merged_pr_not_unique': ('The reviewer finished, but Forge could not identify one merged PR for its checkout.',
                         'Forge must match the checkout to its published PR before releasing this idle session. This is a cleanup problem, not a request for another review.'),
    'resume_requires_changed_work': ('This Codex turn finished and there is no changed assignment to resume.',
                         'Check its completion and slot-release result. Forge should release completed work before starting the next issue.'),
    'resume_requires_completed_execution': ('The previous Codex execution has not been verified as finished.',
                         'Inspect the current worker result and remaining processes before retrying.'),
    'resume_thread_unavailable': ('Forge cannot identify the prior Codex conversation to resume.',
                         'Inspect the saved execution receipt before starting another worker on this issue.'),
    'resume_receipt_mismatch': ('The saved Codex conversation does not match this worker.',
                         'Reconcile the current worker and saved execution before retrying; Forge has preserved the checkout.'),
    'resume_requires_completed_execution_and_changed_work': ('The Codex turn ended, but its recorded work has not changed.',
                         'Inspect its last result and cleanup decision. Finished work should release the slot; unresolved work needs a recorded blocker before a retry.'),
    'fresh_role_work_unavailable': ('The supervisor cannot verify fresh work for this execution.',
                         'Check repository snapshot freshness and access. Forge will retry after reconciliation.'),
    'not_empty_shell_prompt': ('The worker shell contains input and cannot safely accept another command.',
                         'Inspect the worker terminal and resolve the pending input without overwriting it.'),
    'shell_has_child_work': ('A child process still owns work in this session.',
                         'Inspect the remaining process and its result before resuming or releasing the worker.'),
    'processes_not_quiescent': ('Background processes still prevent worker cleanup.',
                         'Inspect the remaining worker jobs for a completed build or a stalled wait. Preserve running work until it finishes.'),
    'not_explicitly_idle': ('The worker has not reached a verified empty prompt for cleanup.',
                         'Inspect pending input, provider dialogs, and current worker activity before releasing the slot.'),
    'session_not_claude': ('The session is no longer running the expected worker application.',
                         'Inspect its startup or exit output and preserve any unfinished checkout before recovery.'),
    'worktree_not_clean': ('Uncommitted or untracked files prevent worker cleanup.',
                          'Inspect the worker checkout. Publish intended work or preserve generated files before retrying cleanup.'),
    'claim_not_unique': ('Issue claims cannot be matched to a verified published handoff.',
                         'Inspect the current claims and linked PRs. Every retained claim needs a verified handoff.'),
    'multi_claim_handoff_incomplete_or_ambiguous': ('Not every issue claim has one verified published handoff.',
                         'Check the claimed issues and their canonical PR links; resolve missing or ambiguous handoffs.'),
    'verification_timeout': ('Cleanup verification exceeded its time limit.',
                         'Check GitHub connectivity and verifier timing. Forge will retry without releasing unverified work.'),
    'verification_deadline_exceeded': ('Cleanup verification exceeded its time limit.',
                         'Check GitHub connectivity and verifier timing. Forge will retry without releasing unverified work.'),
    'read_failed': ('A Git or GitHub read needed for cleanup failed.',
                         'Inspect the cleanup evidence and repository access. Forge will retry the proof.'),
    'verification_unavailable': ('Forge could not verify that this worker can be released.',
                         'Inspect the worker checkout and recent supervisor activity. The release proof will be retried.'),
    'worktree_not_published': ('The worker checkout is not verified as published.',
                         'Inspect local commits and the linked PR before releasing this worker.'),
    'supervisor_lock_required': ('Cleanup did not hold the supervisor lock.',
                         'Inspect the supervisor invocation; cleanup must run under its exclusive lifecycle lock.'),
}


def _epoch(value):
    try:
        value = (datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
                 if isinstance(value, str) else float(value))
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def generation(raw):
    fields = raw.strip().split('|') if isinstance(raw, str) else []
    if (len(fields) != 4 or not re.fullmatch(r'\$\d+', fields[0])
            or not fields[1].isdigit() or not re.fullmatch(r'%\d+', fields[2])
            or not fields[3].isdigit()):
        return None
    return {'id': '|'.join(fields), 'started_at': int(fields[1])}


def observe_generation(session, run=subprocess.run):
    try:
        result = run(['tmux', 'list-panes', '-s', '-t', '=' + session, '-F',
                      '#{session_id}|#{session_created}|#{pane_id}|#{pane_pid}'],
                     capture_output=True, text=True, timeout=2)
        return generation(result.stdout) if result.returncode == 0 and len(result.stdout.splitlines()) == 1 else None
    except (OSError, subprocess.SubprocessError):
        return None


def record_decision(state, session, repo, role, phase, result, *, identity=None, now=None, exit_code=None):
    if (phase not in PHASES or not re.fullmatch(r'[A-Za-z0-9_.-]{1,120}', session)
            or role not in {'build', 'review', 'audit'}):
        raise ValueError('Invalid lifecycle decision identity')
    now = time.time() if now is None else now
    if _epoch(now) is None:
        raise ValueError('Invalid observation time')
    if identity is not None and (not isinstance(identity, dict) or generation(identity.get('id')) != identity):
        identity = None
    reason = result.get('reason') if isinstance(result, dict) else None
    if exit_code in {124, 137}:
        reason = 'verification_timeout'
    if not isinstance(reason, str) or not re.fullmatch(r'[A-Za-z0-9_]{1,100}', reason):
        reason = 'verification_unavailable'
    released = isinstance(result, dict) and result.get('released') is True and exit_code in {0, None}
    outcome = 'released' if released else 'retained' if identity else 'unknown'
    directory = Path(state) / 'lifecycle'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'decisions.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status': 'decision_writer_busy'}
        path = directory / (session + '.json')
        try:
            previous = json.loads(path.read_text())
        except (OSError, ValueError):
            previous = {}
        if not isinstance(previous, dict):
            previous = {}
        prior_at = _epoch(previous.get('observed_at'))
        same = (prior_at is not None and 0 <= now - prior_at <= 300
                and previous.get('generation') == identity and previous.get('phase') == phase
                and previous.get('reason') == reason and previous.get('outcome') == outcome)
        since = _epoch(previous.get('since')) if same else now
        if since is None or since > now:
            since = now
        if identity:
            since = max(since, identity['started_at'])
        value = {'session': session, 'repo': repo, 'role': role, 'phase': phase,
                 'reason': reason, 'outcome': outcome, 'generation': identity,
                 'observed_at': now, 'since': since,
                 'observations': min(1000000, previous['observations'] + 1)
                 if same and type(previous.get('observations')) is int else 1}
        atomic_json(path, value)
        if not same:
            from .activity import record
            record(state, 'lifecycle_decision', session, repo, role,
                   {'phase': phase, 'outcome': outcome, 'reason': reason,
                    'generation_id': identity['id'] if identity else 'unknown', 'since': since}, now=now)
        return value


def retained_blocker(state, session, row, role, activity, now):
    """Never carry a prior generation or stale cleanup alert into a new worker."""
    if not row.get(role, {}).get('up') or row.get(role, {}).get('busy'):
        return None
    try:
        item = json.loads((Path(state) / 'lifecycle' / (session + '.json')).read_text())
        at, since = _epoch(item.get('observed_at')), _epoch(item.get('since'))
        observed = _epoch(activity.get('observed_at'))
        worker = activity.get('sessions', {}).get(session, {})
        birth = _epoch(worker.get('session_started_at'))
        identity = item.get('generation') or {}
        if (item.get('session') != session or item.get('repo') != row['repo'] or item.get('role') != role
                or item.get('outcome') != 'retained' or item.get('phase') not in PHASES
                or at is None or since is None or observed is None or birth is None
                or not 0 <= now - at <= 300 or not 0 <= now - observed <= 90
                or since > at or now - since < 120 or item.get('observations', 0) < 2
                or birth != identity.get('started_at') or generation(identity.get('id')) != identity
                or since < birth or worker.get('generation_id') and worker['generation_id'] != identity['id']
                or worker.get('state') not in {'idle', 'blocked'}):
            return None
        reason = item.get('reason')
        if reason in QUIET_REASONS or reason in LONG_WAIT_REASONS and now - since < 1200:
            return None
        why, next_action = EXPLANATIONS.get(reason, (
            'The worker remains in a slot because its cleanup proof is incomplete.',
            'Inspect the worker and linked PR evidence. Forge will retry cleanup when the proof can be verified.'))
        return {'kind': 'cleanup_retained', 'reason': reason, 'why': why, 'next': next_action,
                'impact': 'This idle worker still occupies a slot and may delay queued work.',
                'observed_at': datetime.fromtimestamp(at, timezone.utc).isoformat().replace('+00:00', 'Z'),
                'since': since, 'basis': 'Repeated current-generation cleanup decisions and a fresh idle worker observation',
                'evidence': {'phase': item['phase'], 'reason': reason,
                             'generation_id': identity['id'], 'observations': item['observations']}}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def progress_key(state, repo, role, now=None):
    """Local queue evidence for retry budgets, never proof of completed work."""
    now = time.time() if now is None else now
    try:
        snapshot = json.loads(snapshot_path(state, repo).read_text())
        at = _epoch(snapshot.get('observed_at'))
        if (snapshot.get('repo') != repo or snapshot.get('status') != 'ok'
                or snapshot.get('enabled') is not True or at is None or not 0 <= now-at <= 300):
            return None
        work = snapshot['work']
        keys = {'build': ('wip', 'sentback', 'limbo'), 'review': ('review',), 'audit': ('audit',)}[role]
        queues = {key: sorted(work.get(key, [])) for key in keys}
        if role == 'build':
            queues['claimed'] = sorted(snapshot['schedule']['active'])
            # A worker without a claim can only acquire the scheduler's next item.
            if not queues['claimed']:
                queues['next'] = list(snapshot['schedule'].get('ordered', []))[:1]
        if any(type(n) is not int or n <= 0 for values in queues.values() for n in values):
            return None
        numbers = sorted({n for values in queues.values() for n in values})
        details = snapshot.get('item_details', {})
        items = {}
        for number in numbers:
            item = details.get(str(number)) or {}
            items[str(number)] = {'updated_at': item.get('updated_at'),
                                  'labels': sorted(item.get('labels', [])),
                                  'state': item.get('state')}
        payload = {'role': role, 'queues': queues, 'items': items}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True)
    parser.add_argument('--config', required=True)
    sub = parser.add_subparsers(dest='action', required=True)
    progress = sub.add_parser('progress')
    progress.add_argument('--session', required=True)
    decision = sub.add_parser('decision')
    decision.add_argument('--session', required=True)
    decision.add_argument('--phase', choices=sorted(PHASES), required=True)
    decision.add_argument('--result', required=True)
    decision.add_argument('--generation')
    decision.add_argument('--exit-code', type=int)
    args = parser.parse_args()
    allowed = {r.short + '-' + role: (r.repo, role) for r in load_repositories(args.config) if r.enabled
               for role in ('build', 'review', 'audit')}
    if args.session not in allowed:
        parser.error('Session is not an enabled configured Forge worker')
    if args.action == 'progress':
        value = progress_key(args.state, *allowed[args.session])
        if value is None:
            return 1
        print(value)
        return 0
    try:
        result = json.loads(args.result)
    except ValueError:
        result = {}
    identity = generation(args.generation) if args.generation is not None else observe_generation(args.session)
    repo, role = allowed[args.session]
    record_decision(args.state, args.session, repo, role, args.phase, result,
                    identity=identity, exit_code=args.exit_code)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
