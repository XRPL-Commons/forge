"""External reset of disposable containers. No terminal or PID heuristics.

The controller owns the engine socket; it is never mounted into a job. Every
execution gets a new container, PID namespace, filesystem and anonymous volume.
The engine's namespace teardown kills detached children as well as the harness.
"""
import io
import json
import os
from pathlib import Path
import re
import selectors
import tempfile
import time
import subprocess
import tarfile
import hashlib
from datetime import datetime

from .capacity import observe as observe_capacity, memory_bytes, launch_lock
from .contracts import assignment_note, identifier, unique
from ..harnesses.registry import manifest
from ..credentials import Credentials


class RunnerError(RuntimeError):
    pass


class RunnerCapacity(RunnerError):
    def __init__(self, decision):
        super().__init__('Waiting for available memory; Forge will retry automatically')
        self.decision = decision


class RunnerUnavailable(RunnerError):
    pass


def process_isolation(config):
    """One launch policy shared by installation canaries and real workers."""
    args = []
    if manifest(config.get('provider', 'codex')).get('private_proc'):
        if config.get('apparmor_profile') != 'forge-runner-private-proc':
            raise RunnerError('This harness requires the Forge private-proc AppArmor policy')
        args += ['--security-opt', 'systempaths=unconfined']
    if config.get('apparmor_profile'):
        args += ['--security-opt', 'apparmor=' + config['apparmor_profile']]
    return args


def scrub(text, secrets=()):
    for value in secrets:
        if value:
            text = text.replace(value, '[REDACTED]')
    text = re.sub(r'\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{16,})\b', '[REDACTED]', text)
    return text


class ContainerRunner:
    def __init__(self, config, *, run=subprocess.run, environ=None):
        self.config, self.run = config, run
        self.environ = os.environ if environ is None else environ

    def _bounded(self, argv, env, payload, timeout, limit, combine=False):
        # Worker-controlled result files must not fill controller memory or disk.
        with tempfile.TemporaryFile() as source:
            if payload:
                source.write(payload)
            source.seek(0)
            process = subprocess.Popen(argv, stdin=source, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT if combine else subprocess.DEVNULL, env=env, close_fds=True)
            chunks, size, deadline = [], 0, time.monotonic() + timeout
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not selector.select(remaining):
                            raise RunnerError('Container operation timed out; reconcile its outcome')
                        block = os.read(process.stdout.fileno(), min(65536, limit + 1 - size))
                        if not block:
                            break
                        size += len(block)
                        if size > limit:
                            raise RunnerError('Container output exceeded its evidence bound')
                        chunks.append(block)
                code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
                return subprocess.CompletedProcess(argv, code, b''.join(chunks), b'')
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                process.stdout.close()

    def call(self, args, *, input=None, timeout=30, check=True, max_output=4*1024*1024, combine=False):
        # Engine address is explicit. Do not inherit Docker contexts, credentials,
        # provider tokens, shell hooks or dynamic-loader overrides.
        env = {k: self.environ[k] for k in ('PATH', 'HOME', 'XDG_RUNTIME_DIR') if k in self.environ}
        try:
            argv = [self.config['engine'], '--host', 'unix://' + self.config['engine_socket'], *args]
            p = (self._bounded(argv, env, input, timeout, max_output, combine) if self.run is subprocess.run else
                 self.run(argv, input=input, capture_output=True, timeout=timeout, env=env))
        except (OSError, subprocess.SubprocessError):
            raise RunnerError('Container operation outcome is unknown; reconcile before replacement') from None
        if check and p.returncode:
            raise RunnerError('Container engine operation failed; capacity remains reserved')
        return p

    @staticmethod
    def name(job):
        return 'forge-job-' + identifier(job['id'])

    def inspect(self, job):
        name = self.name(job)
        # Successful inventory distinguishes absence from a dead/inaccessible engine.
        p = self.call(['container', 'ls', '-a', '--no-trunc', '--filter', 'name=^/' + name + '$', '--format', '{{json .}}'])
        try:
            rows = [json.loads(line) for line in p.stdout.splitlines()]
            if not rows:
                return None
            if len(rows) != 1 or rows[0]['Names'] != name:
                raise ValueError()
            values = json.loads(self.call(['container', 'inspect', rows[0]['ID']]).stdout)
            value = values[0]
            if (len(values) != 1 or value['Name'] != '/' + name
                    or value['Config']['Labels'].get('forge.execution') != job['id']
                    or value['Config']['Labels'].get('forge.slot') != job['slot']
                    or (job.get('container_id') and value['Id'] != job['container_id'])):
                raise ValueError()
            started = value['State'].get('StartedAt')
            started_at = None
            if started and not started.startswith('0001-'):
                try:
                    started_at = datetime.fromisoformat(started.replace('Z', '+00:00')).timestamp()
                except (ValueError, TypeError):
                    pass
            return {'id': value['Id'], 'status': value['State']['Status'],
                    'exit_code': value['State']['ExitCode'], 'running': value['State']['Running'],
                    'oom_killed': value['State'].get('OOMKilled', False),
                    'started_at': started_at}
        except (ValueError, KeyError, TypeError, IndexError):
            raise RunnerError('Container ownership or observation is unverified') from None

    def ready(self):
        self.call(['info', '--format', '{{.ServerVersion}}'])
        try:
            image = json.loads(self.call(['image', 'inspect', self.config['image']]).stdout)
            if image[0]['Config'].get('Labels', {}).get('org.forge.build-environment') != '1':
                raise ValueError()
        except (ValueError, KeyError, TypeError, IndexError):
            raise RunnerError('Runner image lacks the build-environment contract. Rebuild and qualify it using docs/installation.md.') from None
        if self.config.get('seccomp_profile_env'):
            path = Path(self.environ.get(self.config['seccomp_profile_env'], ''))
            if not path.is_absolute() or not path.is_file():
                raise RunnerError('Provision the runner seccomp policy before dispatch')
        try:
            network = json.loads(self.call(['network', 'inspect', self.config['network']]).stdout)
            if len(network) != 1 or network[0].get('Internal') is not True:
                raise ValueError()
        except (ValueError, TypeError, IndexError):
            raise RunnerError('Runner network must be internal; use the egress proxy') from None
        return True

    def create(self, job):
        try:
            with launch_lock(self.config['engine_socket']):
                old = self.inspect(job)
                if old:
                    return old['id']
                decision = self.capacity()
                if not decision['allowed']:
                    raise RunnerCapacity(decision)
                return self._create(job)
        except OSError:
            raise RunnerCapacity({'allowed': False, 'reason': 'telemetry_unavailable', 'at': time.time()}) from None

    def _create(self, job):
        old = self.inspect(job)
        if old:
            return old['id']
        config = self.config
        args = ['create', '--pull=never', '--name', self.name(job), '--label', 'forge.execution=' + job['id'],
                '--label', 'forge.slot=' + job['slot'], '--init', '--read-only',
                '--user', '1000:1000', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '--pids-limit', str(config['pids_limit']), '--memory', config['memory'],
                '--memory-swap', config['memory'], '--cpus', str(config['cpus']),
                '--log-driver', 'json-file', '--log-opt', 'max-size=1m', '--log-opt', 'max-file=2',
                '--network', config['network'], '--ipc', 'private', '--tmpfs', '/tmp:rw,nosuid,nodev,size=256m',
                '--mount', 'type=volume,destination=/work', '--workdir', '/work',
                '--env', 'HOME=/work/home', '--entrypoint', '/usr/local/bin/python3',
                config['image'], '-I', '/opt/forge/job.py']
        args[1:1] = process_isolation(config)
        if config.get('seccomp_profile_env'):
            seccomp = Path(self.environ.get(config['seccomp_profile_env'], ''))
            if not seccomp.is_absolute() or not seccomp.is_file():
                raise RunnerError('Runner seccomp policy is unavailable')
            args[1:1] = ['--security-opt', 'seccomp=' + str(seccomp)]
        self.call(args)
        current = self.inspect(job)
        if current is None or current['status'] != 'created':
            raise RunnerError('Container creation outcome is unknown')
        return current['id']

    def prepare(self, job, token):
        current = self.inspect(job)
        if not current or current['status'] != 'created' or job['fenced']:
            raise RunnerError('Only an unstarted owned execution can receive an assignment')
        credentials = {name: self.environ[name] for name in self.config['provider_environment'] if self.environ.get(name)}
        if len(credentials) != len(self.config['provider_environment']):
            raise RunnerUnavailable('Provider credentials unavailable')
        auth_ref = self.config.get('provider_auth_file_env')
        if auth_ref:
            env = (Credentials(self.environ['FORGE_CREDENTIAL_ROOT']).environment()
                   if self.environ.get('FORGE_CREDENTIAL_ROOT') else self.environ)
            auth_path = Path(env.get(auth_ref, ''))
            if not auth_path.is_absolute() or not auth_path.is_file() or auth_path.stat().st_size > 65536:
                raise RunnerUnavailable('Provider authentication file is unavailable')
            try:
                raw = auth_path.read_bytes()
                credentials['FORGE_HARNESS_AUTH'] = json.loads(raw)
                credentials['FORGE_AUTH_DIGEST'] = hashlib.sha256(raw).hexdigest()
            except (OSError, ValueError):
                raise RunnerUnavailable('Provider authentication file could not be loaded') from None
        credentials['GH_TOKEN'] = token
        note = assignment_note(job)
        note.update(harness_argv=self.config['harness_argv'], grace_seconds=self.config['grace_seconds'],
                    proxy=self.config['proxy'], provider=self.config.get('provider', 'codex'), model=self.config.get('model'))
        content = io.BytesIO()
        with tarfile.open(fileobj=content, mode='w') as archive:
            for name, value in [('assignment.json', note), ('credentials.json', credentials)]:
                raw = json.dumps(value).encode()
                member = tarfile.TarInfo(name)
                member.size, member.mode, member.uid, member.gid = len(raw), 0o600, 1000, 1000
                archive.addfile(member, io.BytesIO(raw))
        self.call(['cp', '-a', '-', current['id'] + ':/work/'], input=content.getvalue())

    def refresh_auth(self, current):
        ref, root = self.config.get('provider_auth_file_env'), self.environ.get('FORGE_CREDENTIAL_ROOT')
        if not ref or not root:
            return 'not_managed'
        def read(path):
            raw = self.call(['cp', current['id'] + ':' + path, '-'], max_output=131072).stdout
            with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
                entries = archive.getmembers()
                if len(entries) != 1 or not entries[0].isfile() or entries[0].size > 65536:
                    raise ValueError('Invalid authentication artifact')
                return archive.extractfile(entries[0]).read().decode()
        try:
            expected = read('/work/home/.forge-auth-origin')
            value = read('/work/home/' + manifest(self.config.get('provider', 'codex'))['auth_file'])
            if not re.fullmatch('[a-f0-9]{64}', expected) or not isinstance(json.loads(value), dict):
                raise ValueError('Invalid refreshed authentication')
            if hashlib.sha256(value.encode()).hexdigest() == expected:
                return 'unchanged'
            return 'saved' if Credentials(root).refresh(ref, expected, value) else 'superseded'
        except (RunnerError, OSError, ValueError, tarfile.TarError, UnicodeError):
            # Lost credentials must never block capacity cleanup. A later login
            # failure is an availability wait and shown as a reconnect request.
            return 'unavailable'

    def capacity(self, job=None):
        try:
            ids = self.call(['container', 'ls', '-a', '--no-trunc', '--filter',
                             'label=forge.execution', '--format', '{{.ID}}']).stdout.decode().splitlines()
            if any(not re.fullmatch(r'[a-f0-9]{64}', value) for value in ids) or len(set(ids)) != len(ids):
                raise ValueError('Invalid worker inventory')
            containers = json.loads(self.call(['container', 'inspect', *ids]).stdout) if ids else []
            if len(containers) != len(ids) or {c['Id'] for c in containers} != set(ids):
                raise ValueError('Incomplete worker inventory')
            requested = memory_bytes(self.config['memory'])
            if job:
                own = [c for c in containers if c['Name'] == '/' + self.name(job)]
                if own:
                    if len(own) != 1 or own[0]['Config']['Labels'].get('forge.execution') != job['id']:
                        raise ValueError('Worker ownership changed')
                    requested = own[0]['HostConfig']['Memory']
                    containers.remove(own[0])
            return observe_capacity(requested, containers)
        except (RunnerError, OSError, ValueError, TypeError, KeyError, UnicodeError):
            return {'allowed': False, 'reason': 'telemetry_unavailable', 'at': time.time()}

    def start(self, job):
        if job['fenced']:
            raise RunnerError('Execution authority was revoked')
        try:
            with launch_lock(self.config['engine_socket']):
                current = self.inspect(job)
                if not current or current['status'] != 'created':
                    raise RunnerError('Start requires the exact unstarted container')
                decision = self.capacity(job)
                if not decision['allowed']:
                    raise RunnerCapacity(decision)
                self.call(['start', current['id']])
        except OSError:
            raise RunnerCapacity({'allowed': False, 'reason': 'telemetry_unavailable', 'at': time.time()}) from None

    def observe(self, job, current=None):
        """Bounded running output is an observation, never a completion result."""
        current = current or self.inspect(job)
        observation = {'at': time.time(), 'execution': current, 'logs': '', 'missing': []}
        if current is None:
            observation['missing'].append('container')
            return observation
        secrets = [self.environ.get(name, '') for name in self.config['provider_environment']]
        try:
            observation['logs'] = scrub(self.call(['logs', '--tail', '100', current['id']],
                combine=True, timeout=5, max_output=262144).stdout.decode(errors='replace')[-65536:], secrets)
        except RunnerError:
            observation['missing'].append('running output')
        return observation

    def capture(self, job):
        current = self.inspect(job)
        if current is None:
            return {'result': None, 'logs': '', 'missing': ['container and worker-local evidence']}
        captured = {'result': None, 'logs': '', 'missing': []}
        secrets = [self.environ.get(name, '') for name in self.config['provider_environment']]
        try:
            raw = self.call(['cp', current['id'] + ':/work/result.json', '-'], max_output=131072).stdout
            if len(raw) > 131072:
                raise ValueError('Oversized result')
            with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
                entries = archive.getmembers()
                if len(entries) != 1 or not entries[0].isfile() or entries[0].size > 65536:
                    raise ValueError('Invalid result artifact')
                captured['result'] = json.loads(scrub(archive.extractfile(entries[0]).read().decode(), secrets), object_pairs_hook=unique)
        except (RunnerError, ValueError, tarfile.TarError, UnicodeError):
            captured['missing'].append('valid result artifact')
        try:
            captured['logs'] = scrub(self.call(['logs', '--tail', '1000', current['id']], combine=True).stdout.decode(errors='replace')[-65536:], secrets)
        except RunnerError:
            captured['missing'].append('runner logs')
        captured['execution'] = current
        return captured

    def reset(self, job):
        if not job['fenced']:
            raise RunnerError('Persist fencing before external reset')
        current = self.inspect(job)
        auth_refresh = 'not_available'
        if current:
            if current['running']:
                # An unsuccessful graceful stop still goes through forced teardown.
                try:
                    self.call(['stop', '--time', str(self.config['grace_seconds']), current['id']],
                              timeout=self.config['grace_seconds'] + 15, check=False)
                except RunnerError:
                    pass  # Force removal still runs after a hung graceful stop.
            if self.config.get('provider_auth_file_env') and self.environ.get('FORGE_CREDENTIAL_ROOT'):
                stopped = self.inspect(job)
                if stopped and not stopped['running']:
                    auth_refresh = self.refresh_auth(stopped)
            self.call(['rm', '--force', '--volumes', current['id']], timeout=30)
        if self.inspect(job) is not None:
            raise RunnerError('Teardown remains unverified')
        self.call(['info', '--format', '{{.ServerVersion}}'])
        return {'container_absent': True, 'capacity_ready': True, 'auth_refresh': auth_refresh}
