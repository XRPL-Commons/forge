"""Recover a never-dispatched worker startup and retain provider diagnostics."""
import argparse
from datetime import datetime
import json
import math
import os
import shlex
import sys
from pathlib import Path
import re
import sqlite3
import subprocess
import time

from .config import load_repositories
from .control import atomic_json, snapshot_path
from .handoff import Unverified, command
from .human_wait import _locked, _clear_verified_no_progress


def _evidence(config, state, session, run, now):
    began = time.monotonic()
    role = session.rsplit('-', 1)[-1]
    if role not in {'build', 'review', 'audit'}:
        raise Unverified('unsupported_startup_role')
    repos = [r for r in load_repositories(config) if r.enabled and r.short + '-' + role == session]
    if len(repos) != 1:
        raise Unverified('not_enabled_worker')
    repo = repos[0]
    if (Path(state) / (session + '.up')).exists():
        raise Unverified('startup_already_completed')
    snap = json.loads(snapshot_path(state, repo.repo).read_text())
    observed = snap.get('observed_at')
    if (snap.get('repo') != repo.repo or snap.get('enabled') is not True or snap.get('status') != 'ok'
            or type(observed) not in (int, float) or not math.isfinite(observed)
            or not 0 <= now - observed <= 300):
        raise Unverified('snapshot_not_fresh')
    if role == 'build':
        if (snap['schedule'].get('active') != [] or not snap['schedule'].get('eligible')
                or snap['counts'].get('wip') != 0
                or any(snap['work'].get(key) != [] for key in ('wip', 'review', 'audit', 'sentback', 'limbo'))):
            raise Unverified('existing_or_unknown_work')
    elif (not snap['work'].get(role) or snap['counts'].get(role, 0) <= 0
          or any(type(n) is not int or n <= 0 for n in snap['work'][role])):
        raise Unverified('no_verified_' + role + '_demand')
    clone = Path(getattr(repo, 'clone_' + role)
                 or Path.home() / (repo.short if role == 'build' else repo.short + '-' + role)).resolve()
    git = ['git', '-C', str(clone)]
    if Path(command(git + ['rev-parse', '--show-toplevel'], run)).resolve() != clone:
        raise Unverified('clone_mismatch')
    if command(git + ['remote', 'get-url', 'origin'], run) not in {
            f'git@github.com:{repo.repo}.git', f'https://github.com/{repo.repo}.git', f'https://github.com/{repo.repo}'}:
        raise Unverified('remote_mismatch')
    if command(git + ['symbolic-ref', '--short', 'HEAD'], run) != 'main':
        raise Unverified('not_main')
    if command(git + ['status', '--porcelain=v1', '--untracked-files=all'], run):
        raise Unverified('worktree_not_clean')
    head = command(git + ['rev-parse', 'HEAD'], run)
    main = json.loads(command(['gh', 'api', f'repos/{repo.repo}/git/ref/heads/main'], run))['object']['sha']
    if not re.fullmatch(r'[0-9a-f]{40,64}', head) or head != main:
        raise Unverified('not_published_main')
    pane = command(['tmux', 'list-panes', '-s', '-t', '=' + session, '-F',
                    '#{session_name}|#{session_id}|#{session_created}|#{pane_id}|#{pane_pid}|#{pane_current_command}|#{pane_current_path}'], run)
    rows = pane.splitlines()
    fields = rows[0].split('|') if len(rows) == 1 else []
    if (len(fields) != 7 or fields[0] != session or not re.fullmatch(r'\$\d+', fields[1])
            or not fields[2].isdigit() or not re.fullmatch(r'%\d+', fields[3])
            or not fields[4].isdigit() or fields[5] != 'bash' or Path(fields[6]).resolve() != clone):
        raise Unverified('not_single_clone_shell')
    created, pid = int(fields[2]), int(fields[4])
    if now - created < 60:
        raise Unverified('startup_still_in_grace_period')
    captured = command(['tmux', 'capture-pane', '-p', '-t', fields[3]], run)
    nonblank = [line.rstrip() for line in captured.splitlines() if line.strip()]
    if not nonblank or not re.fullmatch(r'[\w.-]+@[\w.-]+:[^$#\r\n]*[$#]', nonblank[-1]):
        raise Unverified('not_empty_shell_prompt')
    processes = {}
    for line in command(['ps', '-eo', 'pid=,ppid=,comm='], run).splitlines():
        process, parent, name = line.split(None, 2)
        processes[int(process)] = (int(parent), name)
    if processes.get(pid, (None, None))[1] != 'bash' or any(parent == pid for parent, _ in processes.values()):
        raise Unverified('shell_has_child_work')
    path = Path(state) / 'activity.sqlite3'
    db = sqlite3.connect(path.absolute().as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        creation = db.execute("SELECT id,at FROM events WHERE session=? AND event='session_created' ORDER BY id DESC LIMIT 1", (session,)).fetchone()
        if not creation or abs(datetime.fromisoformat(creation[1].replace('Z', '+00:00')).timestamp() - created) > 5:
            raise Unverified('startup_generation_unrecorded')
        for event, metadata in db.execute('SELECT event,metadata FROM events WHERE session=? AND id>?', (session, creation[0])):
            if event in {'dispatch', 'nudge'} or (event == 'activity_changed' and json.loads(metadata).get('state') == 'busy'):
                raise Unverified('startup_was_dispatched_or_active')
    finally:
        db.close()
    if ((Path(state) / (session + '.up')).exists()
            or json.loads(snapshot_path(state, repo.repo).read_text()) != snap
            or now + time.monotonic() - began - observed > 300):
        raise Unverified('startup_evidence_changed')
    if (command(git + ['rev-parse', 'HEAD'], run) != head
            or command(git + ['status', '--porcelain=v1', '--untracked-files=all'], run)):
        raise Unverified('checkout_changed')
    return {'repo': repo.repo, 'role': role, 'session': session, 'session_id': fields[1], 'session_created': created,
            'pane_id': fields[3], 'pane_pid': pid, 'clone': str(clone), 'head': head,
            'creation_event': creation[0]}


def check(config, state, session, *, run=subprocess.run, now=None):
    try:
        evidence = _evidence(config, state, session, run, time.time() if now is None else now)
        return {'eligible': True, 'reason': 'verified_interrupted_startup', 'evidence': evidence}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error, subprocess.SubprocessError) as exc:
        return {'eligible': False, 'reason': str(exc) if isinstance(exc, Unverified) else 'verification_unavailable', 'evidence': {}}


def release(config, state, session, *, lock_fd=8, run=subprocess.run, now=None):
    if not _locked(state, lock_fd):
        return {'eligible': False, 'released': False, 'reason': 'supervisor_lock_required', 'evidence': {}}
    first = check(config, state, session, run=run, now=now)
    if not first['eligible']:
        return {**first, 'released': False}
    second = check(config, state, session, run=run, now=now)
    if not second['eligible'] or first != second or not _locked(state, lock_fd):
        return {'eligible': False, 'released': False, 'reason': 'startup_changed', 'evidence': {}}
    try:
        command(['tmux', 'kill-session', '-t', second['evidence']['session_id']], run)
    except (OSError, ValueError, subprocess.SubprocessError):
        return {**second, 'released': False, 'reason': 'release_failed'}
    result = {**second, 'released': True, 'observed_at': time.time() if now is None else now}
    try:
        result['cleared_no_progress'] = _clear_verified_no_progress(state, session)
        atomic_json(Path(state) / 'startup-recoveries' / (session + '.json'), result)
        from .activity import record
        record(state, 'session_stopped', session, repo=second['evidence']['repo'], role=second['evidence']['role'],
               metadata={'reason': 'verified_interrupted_startup'}, now=now)
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
        result['receipt_error'] = True
    return result


def launch_command(executable, state, session):
    path = Path(executable).resolve()
    if not Path(executable).is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise Unverified('provider_executable_unavailable')
    if not session or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-' for c in session):
        raise Unverified('invalid_session')
    recorder = ['env', 'PYTHONPATH=' + str(Path(__file__).resolve().parent.parent), sys.executable,
                '-m', 'forge.startup', 'record-exit', '--state', str(Path(state).resolve()), '--session', session]
    # The shell remains the pane parent, so normal quiescence proofs still see
    # exactly bash + Claude. No tee/wrapper/background process is introduced.
    return (shlex.join([str(path), '--dangerously-skip-permissions'])
            + '; forge_provider_exit=$?; ' + shlex.join(recorder)
            + ' --exit-code "$forge_provider_exit"')


def record_failure(state, session, *, exit_code=None, startup=True, run=subprocess.run, now=None):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', session):
        raise ValueError('invalid_session')
    path = Path(state) / 'provider-starts' / (session + '.json')
    identity = run(['tmux', 'display-message', '-p', '-t', '=' + session + ':1.1',
                    '#{session_id}|#{session_created}'], capture_output=True, text=True, timeout=5)
    generation = identity.stdout.strip() if identity.returncode == 0 else None
    prior = {}
    try:
        prior = json.loads(path.read_text())
    except (OSError, ValueError):
        pass
    marker = Path(state) / (session + '.up')
    dispatched = False
    if generation:
        try:
            dispatched = marker.stat().st_mtime >= int(generation.split('|')[1])
        except (OSError, ValueError, IndexError):
            pass
    failure = startup or not dispatched
    diagnostic = None
    if failure:
        pane = run(['tmux', 'capture-pane', '-p', '-t', '=' + session + ':1.1', '-S', '-20'],
                   capture_output=True, text=True, timeout=5)
        diagnostic = pane.stdout[-8192:] if pane.returncode == 0 else 'Terminal diagnostic unavailable'
    value = {'session': session, 'generation': generation,
             'observed_at': time.time() if now is None else now,
             'reason': 'provider_start_failed' if failure else 'provider_exited',
             'exit_code': exit_code if exit_code is not None else
                          (prior.get('exit_code') if generation and prior.get('generation') == generation else None),
             'diagnostic': diagnostic}
    atomic_json(path, value)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'release', 'command', 'record-exit', 'record-failure'))
    parser.add_argument('--config')
    parser.add_argument('--executable')
    parser.add_argument('--exit-code', type=int)
    parser.add_argument('--state', required=True)
    parser.add_argument('--session', required=True)
    parser.add_argument('--lock-fd', type=int, default=8)
    args = parser.parse_args()
    if args.action == 'command':
        try:
            print(launch_command(args.executable or '', args.state, args.session))
            return 0
        except (OSError, ValueError):
            return 1
    if args.action in {'record-exit', 'record-failure'}:
        record_failure(args.state, args.session, exit_code=args.exit_code, startup=args.action == 'record-failure')
        return 0
    if not args.config:
        parser.error('--config is required for verification/release')
    value = (release(args.config, args.state, args.session, lock_fd=args.lock_fd)
             if args.action == 'release' else check(args.config, args.state, args.session))
    print(json.dumps(value, sort_keys=True))
    return 0 if value.get('released', value['eligible']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
