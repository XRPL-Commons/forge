"""One bounded Codex execution; receipts describe execution, never issue completion."""
import argparse
import fcntl
import json
import hashlib
import shlex
import math
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import tempfile
import time
import uuid

MAX_PROMPT = 1024 * 1024
MAX_LINE = 2 * 1024 * 1024
MAX_LOG = 64 * 1024 * 1024
EVENTS = {'thread.started', 'turn.started', 'turn.completed', 'turn.failed',
          'item.started', 'item.updated', 'item.completed', 'error'}


class Refused(ValueError):
    pass


def process_identity(pid):
    raw = Path(f'/proc/{pid}/stat').read_text()
    fields = raw[raw.rfind(')') + 2:].split()
    return {'pid': pid, 'state': fields[0], 'parent_pid': int(fields[1]), 'start_ticks': int(fields[19]), 'process_group': int(fields[2]), 'process_session': int(fields[3])}


def remaining_group(group):
    """Retain proof of background processes even if the CLI leader has exited."""
    members = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            item = process_identity(int(entry.name))
        except (FileNotFoundError, ProcessLookupError):
            continue
        if item['process_session'] == group and item['pid'] != group and item['state'] != 'Z':
            members.append(item)
    if len(members) > 256:
        raise Refused('background_inventory_unbounded')
    return members


def parse_generation(raw):
    fields = raw.split('|')
    if (len(fields) != 4 or not re.fullmatch(r'\$\d+', fields[0])
            or not fields[1].isdigit() or not re.fullmatch(r'%\d+', fields[2])
            or not fields[3].isdigit()):
        raise Refused('invalid_generation')
    return {'id': raw, 'session_id': fields[0], 'session_created': int(fields[1]),
            'pane_id': fields[2], 'pane_pid': int(fields[3])}


def verify_generation(session, expected):
    result = subprocess.run(['tmux', 'list-panes', '-s', '-t', '=' + session, '-F',
        '#{session_id}|#{session_created}|#{pane_id}|#{pane_pid}'],
        capture_output=True, text=True, timeout=3)
    if result.returncode or result.stdout.strip() != expected['id']:
        raise Refused('generation_changed')
    current = os.getpid()
    for _ in range(32):
        if current == expected['pane_pid']:
            return
        if current <= 1:
            break
        current = process_identity(current)['parent_pid']
    raise Refused('runner_not_in_verified_pane')


def private_json(path, value):
    fd, name = tempfile.mkstemp(prefix='.receipt-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def command(executable, clone, resume_thread=None):
    args = [str(executable), 'exec', '--json', '--approve-for-me', '-C', str(clone)]
    if resume_thread:
        # Never resume whichever unrelated thread happens to be most recent.
        if str(uuid.UUID(resume_thread)) != resume_thread:
            raise Refused('invalid_resume_thread')
        args += ['resume', resume_thread]
    return args + ['-']


def provider_failure(event):
    """Normalize explicit provider errors only; never expose their raw message."""
    error = event.get('error') if isinstance(event.get('error'), dict) else event
    code = error.get('code') or error.get('type')
    message = str(error.get('message') or '')[:8192].lower()
    if code in {'insufficient_quota', 'quota_exceeded', 'usage_limit_reached', 'usage_limit_exceeded', 'billing_hard_limit_reached'} or re.search(r"you(?:'ve| have) hit your usage limit|usage limit (?:has been )?reached|insufficient quota", message):
        return 'usage_limit'
    if code in {'authentication_error', 'invalid_api_key', 'token_expired'}:
        return 'authentication'
    if code in {'permission_denied', 'permission_error'}:
        return 'permission'
    if code == 'rate_limit_exceeded':
        return 'rate_limit'
    return None


def apply_event(receipt, event):
    if not isinstance(event, dict) or event.get('type') not in EVENTS:
        return
    kind = event['type']
    receipt['last_event'] = kind
    receipt['event_count'] += 1
    if kind == 'thread.started':
        try:
            thread = str(uuid.UUID(event['thread_id']))
        except (KeyError, ValueError, TypeError, AttributeError):
            receipt['protocol_error'] = True
            return
        if receipt.get('thread_id') and receipt['thread_id'] != thread:
            receipt['protocol_error'] = True
        else:
            receipt['thread_id'] = thread
    elif kind == 'item.completed' and isinstance(event.get('item'), dict) and event['item'].get('type') == 'error':
        message = event['item'].get('message')
        if (isinstance(message, str) and len(message) <= 16384
                and message.startswith('Code Mode is unavailable because failed to spawn code-mode host ')
                and 'codex-code-mode-host' in message
                and 'host executable was not found' in message):
            receipt['runtime_failure'] = 'code_mode_host_unavailable'
            receipt['turn_failed'] = True
    elif kind == 'turn.completed':
        receipt['turn_completed'] = True
    elif kind in {'turn.failed', 'error'}:
        receipt['turn_failed'] = True
        receipt['provider_failure'] = provider_failure(event) or receipt.get('provider_failure')
    # Never copy agent text, commands, exception messages, usage or tool payloads.


def _stop_owned(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def run_once(state, session, repo, role, clone, prompt_file, executable, generation,
             *, resume_thread=None, max_seconds=7200, verify=verify_generation):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,120}', session):
        raise Refused('invalid_session')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) or role not in {'build', 'review', 'audit'}:
        raise Refused('invalid_repository_role')
    if not isinstance(max_seconds, (float, int)) or not 0 < max_seconds <= 86400:
        raise Refused('invalid_deadline')
    clone = Path(clone).resolve(strict=True)
    executable = Path(executable).resolve(strict=True)
    if not clone.is_dir() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise Refused('invalid_worker_paths')
    prompt_path = Path(prompt_file)
    with prompt_path.open('rb') as stream:
        prompt = stream.read(MAX_PROMPT + 1)
    if not prompt.strip() or len(prompt) > MAX_PROMPT:
        raise Refused('invalid_prompt')
    identity = parse_generation(generation)
    args = command(executable, clone, resume_thread)
    root = Path(state) / 'codex-workers'
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or root.stat().st_mode & 0o077:
        raise Refused('worker_state_not_private')
    lock_fd = os.open(root / (session + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused('worker_already_running')
        verify(session, identity)
        prior_path = root / (session + '.json')
        if prior_path.exists():
            prior = json.loads(prior_path.read_text())
            if not isinstance(prior,dict) or not isinstance(prior.get('remaining_processes',[]),list):
                raise Refused('prior_receipt_corrupt')
            for recorded in prior.get('remaining_processes', []):
                try:
                    current = process_identity(recorded['pid'])
                    if current['start_ticks'] == recorded['start_ticks'] and current['state'] != 'Z':
                        raise Refused('prior_background_work_running')
                except (FileNotFoundError,ProcessLookupError):
                    pass
        if resume_thread:
            try:
                previous = json.loads((root / (session + '.json')).read_text())
            except (OSError,ValueError):
                raise Refused('resume_receipt_unavailable')
            if (not isinstance(previous,dict) or previous.get('provider') != 'codex'
                    or previous.get('thread_id') != resume_thread or previous.get('repo') != repo
                    or previous.get('role') != role or previous.get('clone') != str(clone)
                    or previous.get('generation') != identity or previous.get('state') not in {'completed','failed'}):
                raise Refused('resume_receipt_mismatch')
        run_id = str(uuid.uuid4())
        logs = root / 'logs' / session / run_id
        logs.mkdir(mode=0o700, parents=True)
        receipt_path = root / (session + '.json')
        now = time.time()
        receipt = {'provider': 'codex', 'session': session, 'repo': repo, 'role': role,
            'clone': str(clone), 'generation': identity, 'run_id': run_id,
            'runner': process_identity(os.getpid()), 'provider_process': None,
            'remaining_processes': [], 'thread_id': resume_thread, 'state': 'running', 'reason': 'execution_starting',
            'started_at': now, 'observed_at': now, 'completed_at': None,
            'last_event': None, 'event_count': 0, 'turn_completed': False,
            'turn_failed': False, 'provider_failure': None, 'protocol_error': False, 'logs_truncated': False,
            'execution_only': True, 'raw_log': str(logs / 'events.jsonl')}
        private_json(receipt_path, receipt)
        process = None
        failure = None
        old_handlers = {}
        cancelled = [False]
        try:
            # Signals request a bounded shutdown of this execution's own group.
            for sig in (signal.SIGTERM, signal.SIGINT):
                old_handlers[sig] = signal.signal(sig, lambda *_: cancelled.__setitem__(0, True))
            verify(session, identity)
            with tempfile.TemporaryFile() as input_stream:
                input_stream.write(prompt)
                input_stream.seek(0)
                process = subprocess.Popen(args, cwd=clone, stdin=input_stream,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            receipt['provider_process'] = process_identity(process.pid)
            receipt['reason'] = 'execution_running'
            private_json(receipt_path, receipt)
            deadline = time.monotonic() + max_seconds
            last_save = time.monotonic()
            buffer = b''
            dropping = False
            sizes = {'stdout': 0, 'stderr': 0}
            with selectors.DefaultSelector() as selector:
                files = {}
                for label, pipe, filename in [('stdout', process.stdout, 'events.jsonl'), ('stderr', process.stderr, 'stderr.log')]:
                    fd = os.open(logs / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    files[label] = os.fdopen(fd, 'wb')
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_READ, label)
                try:
                    while selector.get_map():
                        if cancelled[0] or time.monotonic() >= deadline:
                            failure = 'execution_cancelled' if cancelled[0] else 'execution_deadline_exceeded'
                            _stop_owned(process)
                            break
                        for key, _ in selector.select(timeout=min(1, max(0, deadline-time.monotonic()))):
                            data = os.read(key.fileobj.fileno(), 65536)
                            label = key.data
                            if not data:
                                selector.unregister(key.fileobj)
                                continue
                            remaining = max(0, MAX_LOG - sizes[label])
                            files[label].write(data[:remaining]); files[label].flush()
                            sizes[label] += len(data)
                            receipt['logs_truncated'] |= sizes[label] > MAX_LOG
                            if label != 'stdout':
                                continue
                            buffer += data
                            while b'\n' in buffer:
                                line, buffer = buffer.split(b'\n', 1)
                                if not dropping and len(line) <= MAX_LINE:
                                    try:
                                        apply_event(receipt, json.loads(line))
                                    except (ValueError, UnicodeError):
                                        receipt['protocol_error'] = True
                                else:
                                    receipt['protocol_error'] = True
                                dropping = False
                            if len(buffer) > MAX_LINE:
                                buffer = b''; dropping = True; receipt['protocol_error'] = True
                        if time.monotonic() - last_save >= 1:
                            receipt['observed_at'] = time.time()
                            private_json(receipt_path, receipt)
                            last_save = time.monotonic()
                    if buffer and not failure:
                        try:
                            apply_event(receipt, json.loads(buffer))
                        except (ValueError, UnicodeError):
                            receipt['protocol_error'] = True
                finally:
                    for stream in files.values():
                        stream.close()
            while process.poll() is None:
                if cancelled[0] or time.monotonic() >= deadline:
                    failure = 'execution_cancelled' if cancelled[0] else 'execution_deadline_exceeded'
                    _stop_owned(process)
                    break
                try:
                    process.wait(timeout=min(1, max(0, deadline-time.monotonic())))
                except subprocess.TimeoutExpired:
                    receipt['observed_at'] = time.time()
                    private_json(receipt_path, receipt)
        except (OSError, ValueError, subprocess.SubprocessError):
            failure = 'execution_unavailable'
            if process is not None:
                _stop_owned(process)
        finally:
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)
            if process is not None:
                for pipe in (process.stdout, process.stderr):
                    if pipe:
                        pipe.close()
        code = process.returncode if process is not None else None
        if process is not None:
            try:
                receipt['remaining_processes'] = remaining_group(process.pid)
                if receipt['remaining_processes']:
                    failure = failure or 'execution_background_processes'
            except (OSError,ValueError):
                failure = failure or 'background_inventory_unavailable'
        if receipt.get('runtime_failure') == 'code_mode_host_unavailable':
            failure = failure or 'runtime_unavailable'
        complete = code == 0 and receipt['turn_completed'] and receipt['thread_id'] and not receipt['turn_failed'] and not receipt['protocol_error'] and not failure
        receipt.update(state='completed' if complete else 'failed',
            reason='execution_completed' if complete else (failure or ('provider_' + receipt['provider_failure'] if receipt['provider_failure'] else 'execution_failed' if code else 'execution_incomplete')),
            exit_code=code, observed_at=time.time(), completed_at=time.time())
        if startup_configuration_failure(receipt):
            receipt['reason'] = 'startup_configuration'
        private_json(receipt_path, receipt)
        return receipt


def startup_configuration_failure(receipt):
    """Only the known pre-protocol CLI argument conflict, never a task failure."""
    if (receipt.get('state') != 'failed' or receipt.get('exit_code') != 2
            or receipt.get('thread_id') is not None or receipt.get('event_count') != 0
            or receipt.get('turn_completed') or receipt.get('turn_failed')
            or receipt.get('protocol_error') or receipt.get('remaining_processes')
            or receipt.get('logs_truncated')):
        return False
    try:
        raw = Path(receipt['raw_log'])
        stderr = raw.with_name('stderr.log')
        if raw.is_symlink() or stderr.is_symlink() or raw.stat().st_size != 0 or stderr.stat().st_size > 16384:
            return False
        lines = stderr.read_text().splitlines()
        return any(re.fullmatch(r"error: the argument '--sandbox <SANDBOX_MODE>' cannot be used with '--approve-for-me'", line.strip()) for line in lines)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def prepare_retry(config, state, session, executable, generation, *, run=subprocess.run):
    """Reserve one known pre-thread retry; caller submits under supervisor lock."""
    from . import startup
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,120}', session):
        raise Refused('invalid_session')
    root = Path(state) / 'codex-workers'
    lock_fd = os.open(root / (session + '.lock'), os.O_RDWR | os.O_NOFOLLOW)
    with os.fdopen(lock_fd, 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused('worker_already_running')
        receipt_path = root / (session + '.json')
        receipt_bytes = receipt_path.read_bytes()
        receipt = json.loads(receipt_bytes)
        expected = parse_generation(generation)
        if (receipt.get('provider') != 'codex' or receipt.get('session') != session
                or receipt.get('generation') != expected or not startup_configuration_failure(receipt)):
            raise Refused('retry_not_verified_startup_configuration')
        failed_id = str(uuid.UUID(receipt['run_id']))
        expected_log = root / 'logs' / session / failed_id / 'events.jsonl'
        if Path(receipt['raw_log']) != expected_log or receipt_path.is_symlink():
            raise Refused('retry_log_identity_mismatch')
        reservation = root / ('retry-' + failed_id + '.json')
        if reservation.exists():
            raise Refused('startup_retry_already_prepared')
        info_path = Path(state) / 'codex-dispatches' / (session + '.json')
        info = json.loads(info_path.read_text())
        if any(info.get(k) != receipt.get(k) for k in ('provider','session','repo','role')) or info.get('generation') != generation or info.get('thread_id') is not None:
            raise Refused('retry_dispatch_mismatch')
        repo, role = startup._codex_repo(config, session)
        clone = Path(getattr(repo, 'clone_' + role) or Path.home()/(repo.short if role=='build' else repo.short+'-'+role)).resolve()
        if receipt.get('repo') != repo.repo or receipt.get('role') != role or receipt.get('clone') != str(clone):
            raise Refused('retry_clone_mismatch')
        prompt_path = Path(info['prompt_file'])
        if prompt_path.is_symlink() or prompt_path.parent.resolve() != (Path(state)/'codex-prompts').resolve() or prompt_path.stat().st_size > MAX_PROMPT:
            raise Refused('retry_prompt_unverified')
        prompt = prompt_path.read_bytes()
        # Existing preparation checks exact generation and empty shell/no children twice,
        # and verifies clone remote plus fresh queue without modifying the checkout.
        prepared = startup.codex_command(config, state, session, executable, generation,
                                         prompt.decode().rstrip('\n'), run=run)
        if receipt_path.read_bytes() != receipt_bytes or prompt_path.read_bytes() != prompt:
            raise Refused('retry_evidence_changed')
        args = shlex.split(prepared)
        args[args.index('--prompt-file') + 1] = str(prompt_path)
        private_json(reservation, {'session':session, 'generation':generation,
            'failed_run_id':failed_id, 'reason':'startup_configuration',
            'prompt_sha256':hashlib.sha256(prompt).hexdigest(), 'prepared_at':time.time()})
        return shlex.join(args)


def observe(state, session, *, generation=None, repo=None, now=None, inspect_process=process_identity):
    """Read-only execution observation. This is never permission to release a pane."""
    unknown = {'state':'unknown', 'reason':'codex_receipt_unverified'}
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,120}', session):
        return unknown
    try:
        path = Path(state) / 'codex-workers' / (session + '.json')
        if path.is_symlink() or path.stat().st_size > 65536:
            return unknown
        receipt = json.loads(path.read_text())
        if generation is None:
            result = subprocess.run(['tmux','list-panes','-s','-t','='+session,'-F',
                '#{session_id}|#{session_created}|#{pane_id}|#{pane_pid}'], capture_output=True,text=True,timeout=3)
            if result.returncode:
                return unknown
            generation = result.stdout.strip()
        expected = parse_generation(generation['id'] if isinstance(generation,dict) else generation)
        if (receipt.get('provider') != 'codex' or receipt.get('session') != session
                or receipt.get('generation') != expected or repo and receipt.get('repo') != repo):
            return unknown
        unknown = {**unknown, 'provider':'codex', 'generation_id':expected['id']}
        now = time.time() if now is None else now
        if (type(receipt.get('observed_at')) not in (int,float) or not math.isfinite(receipt['observed_at'])
                or type(receipt.get('started_at')) not in (int,float) or not math.isfinite(receipt['started_at'])
                or not 0 <= receipt['started_at'] <= receipt['observed_at'] or receipt['observed_at'] > now or (receipt.get('state') == 'running' and now-receipt['observed_at'] > 90)):
            return {**unknown,'reason':'codex_receipt_stale'}
        alive = []
        for recorded in [receipt.get('runner'),receipt.get('provider_process')] + receipt.get('remaining_processes',[]):
            if not isinstance(recorded,dict) or type(recorded.get('pid')) is not int or type(recorded.get('start_ticks')) is not int:
                return unknown
            try:
                current = inspect_process(recorded['pid'])
            except (FileNotFoundError, ProcessLookupError):
                alive.append(False)
            else:
                alive.append(current['start_ticks'] == recorded['start_ticks'] and current.get('state') != 'Z')
        safe = {'provider':'codex','generation_id':expected['id'],'run_id':str(uuid.UUID(receipt['run_id'])),
                'thread_id':str(uuid.UUID(receipt['thread_id'])) if receipt.get('thread_id') else None,
                'session_started_at':expected['session_created'], 'started_at':receipt['started_at'], 'observed_at':receipt['observed_at'],
                'provider_failure':receipt.get('provider_failure') if receipt.get('provider_failure') in {'usage_limit','authentication','permission','rate_limit'} else None}
        if any(alive):
            return {**safe,'state':'busy','reason':'codex_execution_running' if all(alive) else 'codex_execution_settling'}
        if receipt.get('state') == 'completed':
            return {**safe,'state':'idle','reason':'codex_execution_completed'}
        if receipt.get('state') == 'failed':
            return {**safe,'state':'blocked','reason':'startup_configuration' if startup_configuration_failure(receipt) else 'runtime_unavailable' if receipt.get('runtime_failure') == 'code_mode_host_unavailable' else 'codex_execution_failed'}
        return {**safe,'state':'blocked','reason':'codex_runner_lost'}
    except (OSError,ValueError,KeyError,TypeError,AttributeError,subprocess.SubprocessError):
        return unknown


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['run','observe','prepare-retry'])
    for name in ('state','session'):
        parser.add_argument('--' + name, required=True)
    for name in ('repo','role','clone','prompt-file','executable','generation'):
        parser.add_argument('--' + name)
    parser.add_argument('--config')
    parser.add_argument('--resume-thread')
    parser.add_argument('--max-seconds', type=float, default=7200)
    parser.add_argument('--state-only', action='store_true')
    args = parser.parse_args()
    if args.action == 'prepare-retry':
        if not all((args.config,args.executable,args.generation)):
            parser.error('prepare-retry requires config, executable and generation')
        try:
            print(prepare_retry(args.config,args.state,args.session,args.executable,args.generation))
            return 0
        except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as exc:
            print(json.dumps({'reason':str(exc) if isinstance(exc,Refused) else 'retry_refused'}))
            return 1
    if args.action == 'observe':
        result = observe(args.state,args.session,generation=args.generation,repo=args.repo)
        print(result['state'] if args.state_only else json.dumps(result,sort_keys=True))
        return 0
    if any(getattr(args,name) is None for name in ('repo','role','clone','prompt_file','executable','generation')):
        parser.error('run requires repo, role, clone, prompt-file, executable and generation')
    try:
        result = run_once(args.state,args.session,args.repo,args.role,args.clone,args.prompt_file,
                          args.executable,args.generation,resume_thread=args.resume_thread,max_seconds=args.max_seconds)
    except (OSError,ValueError,subprocess.SubprocessError):
        print(json.dumps({'provider':'codex','state':'failed','reason':'runner_refused'}))
        return 1
    print(json.dumps({k: result[k] for k in ('provider','state','reason','run_id','thread_id')}))
    return 0 if result['state'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
