"""Private local observations, never task completion or assignment proof."""
import argparse
from datetime import datetime, timezone
import json
import math
import sys
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
import uuid

from .config import load_repositories
from .control import snapshot_path

EVENTS = {'dispatch', 'session_created', 'session_stopped', 'admission_deferred', 'paused', 'nudge',
          'activity_changed', 'pipeline_observed', 'lifecycle_decision'}
META_KEYS = {'reason', 'state', 'command', 'session_started_at', 'claimed', 'queued', 'counts',
             'snapshot_observed_at', 'snapshot_status', 'start_basis', 'run_id', 'capacity',
             'phase', 'outcome', 'generation_id', 'since', 'session_started_epoch', 'provider', 'provider_run_id', 'provider_thread_id',
             'provider_observed_at', 'provider_failure', 'execution_started_at'}


def stamp(value=None):
    if value is None:
        value = time.time()
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def metadata_safe(metadata):
    value = {k: v for k, v in (metadata or {}).items() if k in META_KEYS}
    for key in ('reason', 'state', 'command', 'snapshot_status', 'start_basis', 'run_id', 'phase', 'outcome', 'generation_id', 'provider', 'provider_run_id', 'provider_thread_id', 'provider_failure'):
        if key in value:
            value[key] = re.sub(r'[^a-zA-Z0-9_.:/ -]', '', str(value[key]))[:120]
    if 'generation_id' in (metadata or {}):
        generation = metadata['generation_id']
        value['generation_id'] = generation if isinstance(generation, str) and re.fullmatch(r'\$\d+\|\d+\|%\d+\|\d+', generation) else ''
    for key in ('since', 'session_started_epoch', 'provider_observed_at'):
        if key in value and (type(value[key]) not in (int, float) or not math.isfinite(value[key])):
            del value[key]
    if 'execution_started_at' in value:
        try:
            if type(value['execution_started_at']) not in (int, float, str):
                raise ValueError('Execution start is unknown')
            value['execution_started_at'] = stamp(value['execution_started_at'])
        except (ValueError, TypeError, OverflowError):
            del value['execution_started_at']
    for key in ('claimed', 'queued'):
        if key in value:
            value[key] = sorted({v for v in value[key] if type(v) is int and v > 0})[:100]
    if 'counts' in value:
        value['counts'] = {k: v for k, v in value['counts'].items()
                           if k in {'audit','builder','conflicts','held','limbo','ready','ready_total','review','sentback','wip'} and type(v) is int}
    if 'capacity' in value:
        receipt = value['capacity']
        safe = {}
        for key in ('reason', 'requested_session'):
            if isinstance(receipt.get(key), str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,120}',receipt[key]):
                safe[key] = receipt[key]
        for key in ('admitted','fleet_count','observed_at'):
            if type(receipt.get(key)) in (bool,int,float):
                safe[key] = receipt[key]
        for key,keys in [('role_counts',{'build','review','audit'}),('limits',{'launch_reserve_mib','max_memory_psi_full','max_memory_psi_some','max_sessions','max_sessions_per_role','minimum_available_mib'})]:
            safe[key] = {k:v for k,v in receipt.get(key,{}).items() if k in keys and type(v) in (int,float)}
        value['capacity'] = safe
    if len(json.dumps(value)) > 8192:
        raise ValueError('Activity metadata exceeds 8 KiB')
    return value


def connect(state):
    directory = Path(state)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'activity.sqlite3'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    os.close(fd)
    db = sqlite3.connect(path, timeout=15)
    db.execute('PRAGMA journal_mode=DELETE')
    db.executescript('''CREATE TABLE IF NOT EXISTS events (
      id INTEGER PRIMARY KEY, at TEXT NOT NULL, event TEXT NOT NULL, session TEXT NOT NULL,
      repo TEXT, role TEXT, metadata TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS sessions (session TEXT PRIMARY KEY, data TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS observer (id INTEGER PRIMARY KEY CHECK(id=1), at TEXT NOT NULL, status TEXT NOT NULL);
      CREATE TRIGGER IF NOT EXISTS immutable_events_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'append-only'); END;
      CREATE TRIGGER IF NOT EXISTS immutable_events_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'append-only'); END;''')
    return db


def _record(db, event, session, repo, role, metadata, at):
    row = db.execute('SELECT data FROM sessions WHERE session=?', (session,)).fetchone()
    current = json.loads(row[0]) if row else dict(run_id=None, execution_started_at=None,
        dispatched_at=None, observed_active_since=None, last_activity_at=None,
        state='unknown', session_started_at=None, reason=None, repo=repo, role=role)
    if event == 'lifecycle_decision':
        db.execute('INSERT INTO events(at,event,session,repo,role,metadata) VALUES(?,?,?,?,?,?)',
                   (at,event,session,repo,role,json.dumps(metadata,sort_keys=True)))
        return current
    if event == 'activity_changed':
        birth = metadata.get('session_started_at')
        generation = metadata.get('generation_id')
        barrier = current.get('generation_event_at')
        # Captures can finish after a supervisor replacement. Never apply older evidence.
        if ((generation and generation == current.get('previous_generation_id') and generation != current.get('generation_id')) or
                (barrier and at < barrier) or
                (current.get('last_observed_at') and at < current['last_observed_at']) or
                (current.get('dispatched_at') and at < current['dispatched_at']) or
                (birth and current.get('session_started_at') and birth < current['session_started_at']) or
                (not generation and birth and birth == current.get('previous_session_started_at') and birth != current.get('session_started_at')) or
                (current['state'] == 'exited' and ((generation and generation == current.get('generation_id')) or (not generation and birth and birth == current.get('session_started_at'))) and metadata.get('state') != 'exited') or
                (barrier and at <= barrier and not birth and metadata.get('state') == 'exited')):
            return current
        current['last_observed_at'] = at
    current.update(repo=repo or current.get('repo'), role=role or current.get('role'))
    if event == 'dispatch':
        current.update(run_id=uuid.uuid4().hex, dispatched_at=at, execution_started_at=None,
                       start_basis='supervisor_dispatch', observed_active_since=None)
        metadata['run_id'] = current['run_id']
    elif event == 'session_created':
        current.update(previous_session_started_at=current.get('session_started_at') or current.get('previous_session_started_at'),
                       session_started_at=None, session_started_epoch=None, generation_event_at=at,
                       previous_generation_id=current.get('generation_id') or current.get('previous_generation_id'), generation_id=None)
        current.update(state='starting',run_id=None,dispatched_at=None,execution_started_at=None,
                       observed_active_since=None, provider='unknown', provider_run_id=None, provider_thread_id=None, provider_failure=None)
    elif event == 'activity_changed':
        if ((metadata.get('generation_id') and current.get('generation_id') and metadata['generation_id'] != current['generation_id']) or
                (metadata.get('session_started_at') and current.get('session_started_at')
                 and metadata['session_started_at'] != current['session_started_at'])):
            current['previous_session_started_at'] = current['session_started_at']
            current['previous_generation_id'] = current.get('generation_id')
            current.update(run_id=None,dispatched_at=None,execution_started_at=None,observed_active_since=None)
        if metadata.get('provider') and metadata['provider'] != current.get('provider'):
            current.update(execution_started_at=None,provider_run_id=None,provider_thread_id=None,provider_failure=None)
        new_state = metadata.get('state', 'unknown')
        if new_state == 'busy' and (current['state'] != 'busy' or not current.get('observed_active_since')):
            current['observed_active_since'] = at
        elif new_state != 'busy':
            current['observed_active_since'] = None
        if new_state == 'exited':
            current.update(run_id=None, dispatched_at=None, execution_started_at=None)
        current['state'] = new_state
    elif event == 'session_stopped':
        current['generation_event_at'] = at
        current.update(state='exited', run_id=None, dispatched_at=None, execution_started_at=None,
                       observed_active_since=None)
    for key in ('session_started_at', 'session_started_epoch', 'generation_id', 'command', 'reason', 'provider', 'provider_run_id',
                'provider_thread_id', 'provider_observed_at', 'provider_failure', 'execution_started_at'):
        if key in metadata:
            current[key] = metadata[key]
    current['last_activity_at'] = max(at, current.get('last_activity_at') or at)
    db.execute('INSERT INTO events(at,event,session,repo,role,metadata) VALUES(?,?,?,?,?,?)',
               (at,event,session,repo,role,json.dumps(metadata,sort_keys=True)))
    db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?)', (session,json.dumps(current)))
    return current


def record(state, event, session, repo=None, role=None, metadata=None, now=None):
    if event not in EVENTS or not re.fullmatch(r'[A-Za-z0-9_.-]+', session):
        raise ValueError('Invalid activity event or session')
    db = connect(state)
    try:
        with db:
            return _record(db,event,session,repo,role,metadata_safe(metadata),stamp(now))
    finally:
        db.close()


def read_status(state, allowed_sessions=None):
    path = Path(state) / 'activity.sqlite3'
    if not path.exists():
        return {'status':'unavailable','observed_at':None,'sessions':{},'events':[]}
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        allowed = None if allowed_sessions is None else set(allowed_sessions)
        sessions = {name:json.loads(data) for name,data in db.execute('SELECT session,data FROM sessions')
                    if allowed is None or name in allowed}
        events = []
        for at,event,session,repo,role,metadata in db.execute('SELECT at,event,session,repo,role,metadata FROM events ORDER BY id DESC'):
            if allowed is None or session in allowed:
                events.append(dict(at=at,event=event,session=session,repo=repo,role=role,metadata=json.loads(metadata)))
                if len(events) == 100:
                    break
        row = db.execute('SELECT at,status FROM observer WHERE id=1').fetchone()
        return dict(status=row[1] if row else 'not_sampled',observed_at=row[0] if row else None,sessions=sessions,events=events)
    finally:
        db.close()


def configured(config):
    return {r.short + '-' + role:(r.repo,role) for r in load_repositories(config) if r.enabled
            for role in ('build','review','audit')}


def run_tmux(arguments):
    return subprocess.run(['tmux',*arguments],capture_output=True,text=True,timeout=3)


def classify_pane(command, pane):
    """Classify a bounded provider pane; idle means safe input, never completion."""
    from .handoff import idle_prompt, styled_lines
    if command in {'bash', 'zsh', 'sh', 'fish', 'dash'}:
        return dict(state='shell', reason='shell_prompt')
    if command not in {'claude', 'claude.exe'}:
        return dict(state='unknown', reason='unrecognized_foreground')
    pane = pane[-32768:]
    lines = [''.join(char for char, _ in line) for line in styled_lines(pane)]
    tail = '\n'.join(lines[-16:]).lower()
    if '\x1b' in tail:
        return dict(state='unknown', reason='unrecognized_terminal_control')
    if idle_prompt(pane):
        return dict(state='idle', reason='prompt_observed')
    if tail.rfind('esc to interrupt') >= 0 and tail.rfind('esc to interrupt') > tail.rfind('❯'):
        return dict(state='busy', reason='interrupt_indicator')
    return dict(state='unknown', reason='no_safe_activity_signal')


def observe_tmux(allowed, runner=run_tmux, *, state=None, codex_observer=None):
    """Only capture allowlisted role panes; no raw terminal content is retained."""
    unknown = {s:dict(state='unknown',reason='tmux_unavailable',provider='unknown') for s in allowed}
    try:
        result = runner(['list-sessions','-F','#{session_name}\t#{session_created}\t#{session_id}'])
        if result.returncode:
            # tmux reports absence of its socket/server this way; all other errors are unknown.
            if 'no server running' in result.stderr or ('error connecting to' in result.stderr and 'No such file or directory' in result.stderr):
                return {s:dict(state='exited',reason='no_tmux_server') for s in allowed}
            return unknown
        present = {}
        identities = {}
        for line in result.stdout.splitlines():
            fields = line.split('\t')
            name, started = (fields + ['', ''])[:2]
            if len(fields) >= 3:
                identities[name] = (fields[2], started)
            if name in allowed:
                present[name] = stamp(float(started)) if started.isdigit() else None
        observations = {}
        for session in allowed:
            if session not in present:
                observations[session] = dict(state='exited',reason='session_absent')
                continue
            meta = dict(session_started_at=present[session],provider='unknown')
            if present[session]:
                meta['session_started_epoch'] = int(datetime.fromisoformat(present[session].replace('Z','+00:00')).timestamp())
            try:
                result = runner(['list-panes','-s','-t','='+session,'-F','#{pane_id}\t#{pane_current_command}\t#{pane_dead}\t#{pane_pid}'])
                panes = [line.split('\t') for line in result.stdout.splitlines()]
                if result.returncode or len(panes) != 1 or len(panes[0]) not in (3, 4):
                    meta.update(state='unknown',reason='pane_unavailable_or_ambiguous')
                else:
                    pane,command,dead = panes[0][:3]
                    if session in identities and len(panes[0]) == 4:
                        meta['generation_id'] = '|'.join((*identities[session], pane, panes[0][3]))
                    meta['command'] = command
                    if command in {'claude','claude.exe'}:
                        meta['provider'] = 'claude'
                    if dead == '1':
                        meta.update(state='exited',reason='pane_dead')
                    elif command not in {'claude','claude.exe'} and state is not None and (
                            (Path(state)/'codex-workers'/(session+'.json')).exists() or command in {'codex','codex.exe'}):
                        if codex_observer is None:
                            from .codex_worker import observe as codex_observer
                        if not meta.get('generation_id'):
                            meta.update(state='unknown', reason='codex_generation_unavailable', provider='unknown')
                        else:
                            result = codex_observer(state, session, generation=meta['generation_id'])
                            meta.update(state=result['state'], reason=result['reason'], provider=result.get('provider','unknown'))
                            if result.get('provider') == 'codex':
                                for source, destination in (('run_id','provider_run_id'), ('thread_id','provider_thread_id'),
                                        ('observed_at','provider_observed_at'), ('provider_failure','provider_failure'),
                                        ('started_at','execution_started_at')):
                                    if result.get(source) is not None:
                                        meta[destination] = result[source]
                    elif command in {'bash','zsh','sh','fish','dash'}:
                        meta.update(state='shell',reason='shell_prompt',provider='unknown')
                    else:
                        captured = runner(['capture-pane','-e','-p','-t',pane,'-S','-80'])
                        if captured.returncode:
                            meta.update(state='unknown',reason='capture_failed')
                        else:
                            meta.update(classify_pane(command, captured.stdout))
                            meta['provider'] = 'claude' if command in {'claude','claude.exe'} else 'unknown'
            except (OSError,subprocess.TimeoutExpired):
                meta.update(state='unknown',reason='tmux_timeout_or_error')
            observations[session] = meta
        return observations
    except (OSError,subprocess.TimeoutExpired):
        return unknown


def sample(state, config, now=None, runner=run_tmux):
    allowed = configured(config)
    at = stamp(now)  # Capture start, so late observations cannot overwrite newer lifecycle events.
    observations = observe_tmux(allowed,runner,state=state)
    epoch = datetime.fromisoformat(at.replace('Z','+00:00')).timestamp()
    try:
        guard = json.loads((Path(state)/'legacy-health/guard.json').read_text())
    except (OSError,ValueError):
        guard = {}
    db = connect(state)
    try:
        with db:
            for session,(repo,role) in allowed.items():
                meta = observations[session]
                health = guard.get('sessions',{}).get(session,{})
                if meta['state'] not in {'exited','unknown','busy'}:
                    if guard.get('account_until',0) > epoch:
                        meta.update(state='blocked',reason=guard.get('account_reason','account_cooldown'))
                    elif health.get('until',0) > epoch:
                        meta.update(state='blocked',reason=health.get('reason','cooldown'))
                meta = metadata_safe(meta)
                row = db.execute('SELECT data FROM sessions WHERE session=?',(session,)).fetchone()
                old = json.loads(row[0]) if row else {}
                if any(old.get(k) != v for k,v in meta.items() if k != 'provider_observed_at') or (meta.get('state') == 'busy' and not old.get('observed_active_since')):
                    _record(db,'activity_changed',session,repo,role,meta,at)
                try:
                    snapshot = json.loads(snapshot_path(state,repo).read_text())
                    work = snapshot.get('work',{})
                    association = metadata_safe(dict(claimed=work.get('wip',[]) if role=='build' else [],
                        queued=work.get({'build':'ready','review':'review','audit':'audit'}[role],[]),
                        counts=snapshot.get('counts',{}),snapshot_status=snapshot.get('status','unknown'),
                        snapshot_observed_at=snapshot.get('observed_at')))
                    previous = db.execute("SELECT metadata FROM events WHERE session=? AND event='pipeline_observed' ORDER BY id DESC LIMIT 1",(session,)).fetchone()
                    prior = json.loads(previous[0]) if previous else {}
                    if {k:v for k,v in prior.items() if k != 'snapshot_observed_at'} != {k:v for k,v in association.items() if k != 'snapshot_observed_at'}:
                        _record(db,'pipeline_observed',session,repo,role,association,at)
                except (OSError,ValueError,TypeError,AttributeError):
                    pass
            status = 'degraded' if any(m['state']=='unknown' for m in observations.values()) else 'ok'
            db.execute('INSERT OR REPLACE INTO observer VALUES(1,?,?)',(at,status))
    finally:
        db.close()
    return read_status(state,allowed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state')
    parser.add_argument('--config')
    sub = parser.add_subparsers(dest='command',required=True)
    event = sub.add_parser('event')
    event.add_argument('--session',required=True)
    event.add_argument('--event',required=True,choices=sorted(EVENTS))
    event.add_argument('--reason')
    event.add_argument('--metadata',default='{}')
    sub.add_parser('sample')
    classifier = sub.add_parser('classify')
    classifier.add_argument('--command', dest='foreground', required=True)
    classifier.add_argument('--state-only', action='store_true')
    args = parser.parse_args()
    if args.command == 'classify':
        result = classify_pane(args.foreground, sys.stdin.read(32769))
        print(result['state'] if args.state_only else json.dumps(result, sort_keys=True))
        return 0
    if not args.state or not args.config:
        parser.error('--state and --config are required for event/sample')
    allowed = configured(args.config)
    if args.command == 'sample':
        sample(args.state,args.config)
    else:
        if args.session not in allowed:
            parser.error('Session is not an enabled configured Forge role')
        metadata = json.loads(args.metadata)
        if args.reason:
            metadata['reason'] = args.reason
        repo,role = allowed[args.session]
        if args.event == 'admission_deferred':
            try:
                receipt = json.loads((Path(args.state)/'capacity.json').read_text())
                if receipt.get('requested_session') == args.session:
                    metadata['capacity'] = receipt
            except (OSError,ValueError):
                pass
        record(args.state,args.event,args.session,repo,role,metadata)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
