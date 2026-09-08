"""Reclaim an interrupted, never-dispatched builder startup, not abandoned work."""
import argparse
from datetime import datetime
import json
import math
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
    repos = [r for r in load_repositories(config) if r.enabled and r.short + '-build' == session]
    if len(repos) != 1:
        raise Unverified('not_enabled_builder')
    repo = repos[0]
    if (Path(state) / (session + '.up')).exists():
        raise Unverified('startup_already_completed')
    snap = json.loads(snapshot_path(state, repo.repo).read_text())
    observed = snap.get('observed_at')
    if (snap.get('repo') != repo.repo or snap.get('enabled') is not True or snap.get('status') != 'ok'
            or type(observed) not in (int, float) or not math.isfinite(observed)
            or not 0 <= now - observed <= 300):
        raise Unverified('snapshot_not_fresh')
    if (snap['schedule'].get('active') != [] or not snap['schedule'].get('eligible')
            or snap['counts'].get('wip') != 0
            or any(snap['work'].get(key) != [] for key in ('wip', 'review', 'audit', 'sentback', 'limbo'))):
        raise Unverified('existing_or_unknown_work')
    clone = Path(repo.clone_build or Path.home() / repo.short).resolve()
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
    return {'repo': repo.repo, 'session': session, 'session_id': fields[1], 'session_created': created,
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
        record(state, 'session_stopped', session, repo=second['evidence']['repo'], role='build',
               metadata={'reason': 'verified_interrupted_startup'}, now=now)
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
        result['receipt_error'] = True
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'release'))
    parser.add_argument('--config', required=True)
    parser.add_argument('--state', required=True)
    parser.add_argument('--session', required=True)
    parser.add_argument('--lock-fd', type=int, default=8)
    args = parser.parse_args()
    value = (release(args.config, args.state, args.session, lock_fd=args.lock_fd)
             if args.action == 'release' else check(args.config, args.state, args.session))
    print(json.dumps(value, sort_keys=True))
    return 0 if value.get('released', value['eligible']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
