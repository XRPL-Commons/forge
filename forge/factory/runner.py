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

from .capacity import observe as observe_capacity, memory_bytes, launch_lock
from .contracts import assignment_note, identifier, strategist_note, unique
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


def observation_timeout(deadline, maximum):
    if deadline is None:
        return maximum
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RunnerError('Container observation timed out')
    return min(maximum, remaining)


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
            # Structured provider errors serialize credentials inside JSON.
            # Match those complete encodings before retaining a bounded tail.
            encodings = {value, json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1]}
            for encoded in sorted(encodings, key=len, reverse=True):
                text = text.replace(encoded, '[REDACTED]')
    text = re.sub(r'\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{16,})\b', '[REDACTED]', text)
    # Login files carry OAuth access/refresh tokens as JWTs.
    text = re.sub(r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b', '[REDACTED]', text)
    return text


def secret_values(value, minimum=12):
    """Every string leaf of a login file long enough to be a credential."""
    if isinstance(value, dict):
        return [item for child in value.values() for item in secret_values(child, minimum)]
    if isinstance(value, list):
        return [item for child in value for item in secret_values(child, minimum)]
    return [value] if isinstance(value, str) and len(value) >= minimum else []


def revoke_token(token):
    """Best effort: an installation token revoked at teardown cannot outlive its worker."""
    from ..app_access import AppClient, AccessError
    try:
        AppClient()._request('DELETE', '/installation/token', token)
        return 'revoked'
    except AccessError:
        return 'failed'


class ContainerRunner:
    def __init__(self, config, *, run=subprocess.run, environ=None, revoke=revoke_token):
        self.config, self.run, self.revoke = config, run, revoke
        self.environ = os.environ if environ is None else environ

    def _secrets_path(self, job):
        root = self.environ.get('FORGE_CREDENTIAL_ROOT')
        name = hashlib.sha256(str(job['id']).encode()).hexdigest()[:32] + '.json'
        return Path(root) / 'state/factory/job-secrets' / name if root else None

    def job_secrets(self, job):
        """What this job's worker was given: scrubbed exactly from its output, revoked at reset."""
        path = self._secrets_path(job)
        try:
            return json.loads(path.read_text()) if path else {}
        except (OSError, ValueError):
            return {}

    def _scrub_values(self, job):
        stored = self.job_secrets(job)
        return ([self.environ.get(name, '') for name in self.config['provider_environment']]
                + stored.get('values', []) + [stored.get('github_token') or ''])

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

    def inspect(self, job, *, deadline=None, resources=False):
        name = self.name(job)
        # Successful inventory distinguishes absence from a dead/inaccessible engine.
        p = self.call(['container', 'ls', '-a', '--no-trunc', '--filter', 'name=^/' + name + '$', '--format', '{{json .}}'], timeout=observation_timeout(deadline, 30))
        try:
            rows = [json.loads(line) for line in p.stdout.splitlines()]
            if not rows:
                return None
            if len(rows) != 1 or rows[0]['Names'] != name:
                raise ValueError()
            values = json.loads(self.call(['container', 'inspect', rows[0]['ID']], timeout=observation_timeout(deadline, 30)).stdout)
            value = values[0]
            if (len(values) != 1 or value['Name'] != '/' + name
                    or value['Config']['Labels'].get('forge.execution') != job['id']
                    or value['Config']['Labels'].get('forge.slot') != job['slot']
                    or (job.get('container_id') and value['Id'] != job['container_id'])):
                raise ValueError()
            from .resource_usage import engine_time, stats
            current = {'id': value['Id'], 'status': value['State']['Status'],
                    'exit_code': value['State']['ExitCode'], 'running': value['State']['Running'],
                    'oom_killed': value['State'].get('OOMKilled', False),
                    'started_at': engine_time(value['State'].get('StartedAt')),
                    'finished_at': engine_time(value['State'].get('FinishedAt'))}
            if resources:
                current['resources'] = (stats(self.config['engine_socket'], current['id'], deadline=deadline)
                    if current['running'] else dict(checked_at=time.time(), status='unavailable',
                                                   reason='container_not_running', sample=None))
            return current
        except (ValueError, KeyError, TypeError, IndexError):
            raise RunnerError('Container ownership or observation is unverified') from None

    def ready(self):
        self.call(['info', '--format', '{{.ServerVersion}}'])
        try:
            image = json.loads(self.call(['image', 'inspect', self.config['image']]).stdout)
            if image[0]['Config'].get('Labels', {}).get('org.forge.build-environment') != '1':
                raise ValueError()
            if self.config.get('environment_tier') == 'full' and image[0]['Config'].get('Labels', {}).get('org.forge.services') != 'mongodb,postgres,redis':
                raise ValueError()
        except (ValueError, KeyError, TypeError, IndexError):
            raise RunnerError('Runner image lacks the build-environment contract. Rebuild and qualify it using docs/installation.md.') from None
        if self.config.get('seccomp_profile_env'):
            path = self.seccomp_policy(self.config)
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
            seccomp = self.seccomp_policy(config)
            if not seccomp.is_absolute() or not seccomp.is_file():
                raise RunnerError('Runner seccomp policy is unavailable')
            args[1:1] = ['--security-opt', 'seccomp=' + str(seccomp)]
        self.call(args)
        current = self.inspect(job)
        if current is None or current['status'] != 'created':
            raise RunnerError('Container creation outcome is unknown')
        return current['id']

    def seccomp_policy(self, config):
        """The configured variable, else the policy setup installs in the runtime root."""
        value = self.environ.get(config['seccomp_profile_env'])
        if not value and self.environ.get('FORGE_CREDENTIAL_ROOT'):
            value = str(Path(self.environ['FORGE_CREDENTIAL_ROOT']) / 'runner-seccomp.json')
        return Path(value or '')

    def prepare(self, job, token, *, bundles=()):
        current = self.inspect(job)
        if not current or current['status'] != 'created' or job['fenced']:
            raise RunnerError('Only an unstarted owned execution can receive an assignment')
        credentials = {name: self.environ[name] for name in self.config['provider_environment'] if self.environ.get(name)}
        if len(credentials) != len(self.config['provider_environment']):
            raise RunnerUnavailable('Provider credentials unavailable')
        auth_ref = self.config.get('provider_auth_file_env')
        if auth_ref:
            env = (Credentials(self.environ['FORGE_CREDENTIAL_ROOT']).environment(only=[auth_ref])
                   if self.environ.get('FORGE_CREDENTIAL_ROOT') else self.environ)
            auth_path = Path(env.get(auth_ref, ''))
            if not auth_path.is_absolute() or not auth_path.is_file() or auth_path.stat().st_size > 65536:
                raise RunnerUnavailable('Provider authentication file is unavailable')
            try:
                raw = auth_path.read_bytes()
                credentials['FORGE_HARNESS_AUTH'] = json.loads(raw)
                if self.config.get('provider', 'codex') == 'codex' and self.config.get('codex_parallel_auth'):
                    from .codex_auth import snapshot, renew_in_container
                    root = self.environ.get('FORGE_CREDENTIAL_ROOT')
                    if not root:
                        raise ValueError('Shared login must be installation-owned')
                    credentials['FORGE_HARNESS_AUTH'] = snapshot(root, auth_ref, job['deadline'],
                        lambda directory, **kwargs: renew_in_container(self, directory, **kwargs))
                credentials['FORGE_AUTH_DIGEST'] = hashlib.sha256(raw).hexdigest()
            except (RunnerError, OSError, ValueError, KeyError):
                raise RunnerUnavailable('Provider authentication file could not be loaded or renewed') from None
        credentials['GH_TOKEN'] = token
        path = self._secrets_path(job)
        if path:
            # Written before the worker can see them: capture scrubs these exact
            # values and reset revokes the token, in whichever cycle they run.
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump({'github_token': token, 'values': secret_values(credentials.get('FORGE_HARNESS_AUTH'))}, stream)
        # A job built by a caller that predates roles still gets an assignment.
        role = job.get('role')
        if role == 'triage':
            from .contracts import triage_note
            note = triage_note(job)
        elif role == 'discussion':
            from .discussions import note as discussion_note
            note = discussion_note(job)
        else:
            note = strategist_note(job) if role == 'strategist' else assignment_note(job)
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
        for index, bundle in enumerate(bundles):
            content = io.BytesIO()
            with tarfile.open(fileobj=content, mode='w') as archive:
                raw = bundle['content']
                member = tarfile.TarInfo('input-' + str(index) + '.bundle')
                member.size, member.mode, member.uid, member.gid = len(raw), 0o400, 1000, 1000
                archive.addfile(member, io.BytesIO(raw))
            self.call(['cp', '-a', '-', current['id'] + ':/work/'], input=content.getvalue())

    def refresh_auth(self, current):
        if self.config.get('provider', 'codex') == 'codex' and self.config.get('codex_parallel_auth'):
            # Access-only workers cannot write back into the managed login.
            return 'controller_managed'
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
            if self.config.get('provider', 'codex') == 'codex':
                from .codex_auth import renewable
                if renewable(json.loads(value)):
                    # An access-only worker copy (e.g. launched while parallel auth was on)
                    # must never replace the managed login it was cut from.
                    return 'access_only'
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
            # The host reserve is what stays free for the OS, the engine and the
            # controller; an 8 GB box with three 2 GB workers needs less than 2 GiB.
            return observe_capacity(requested, containers, reserve=memory_bytes(self.config.get('host_reserve', '2g')))
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

    def observe(self, job, current=None, lines=100, timeout=None):
        """Bounded running output is an observation, never a completion result."""
        deadline = time.monotonic() + timeout if timeout is not None else None
        current = current or (self.inspect(job, deadline=deadline) if deadline is not None else self.inspect(job))
        observation = {'at': time.time(), 'execution': current, 'logs': '', 'missing': []}
        if current is None:
            observation['missing'].append('container')
            return observation
        secrets = self._scrub_values(job)
        try:
            observation['logs'] = scrub(self.call(['logs', '--tail', str(int(lines)), current['id']],
                combine=True, timeout=observation_timeout(deadline, 5), max_output=262144).stdout.decode(errors='replace'), secrets)[-65536:]
        except RunnerError:
            observation['missing'].append('running output')
        return observation

    def capture(self, job):
        current = self.inspect(job)
        if current is None:
            return {'result': None, 'logs': '', 'missing': ['container and worker-local evidence']}
        captured = {'result': None, 'logs': '', 'missing': []}
        # A complete repository proposal is normalized into many rows; ordinary
        # worker reports retain their small result boundary.
        from ..harnesses.results import result_limit as limit_for_role
        result_limit = limit_for_role(job.get('role'))
        archive_limit = result_limit + 65536
        secrets = self._scrub_values(job)
        try:
            raw = self.call(['cp', current['id'] + ':/work/result.json', '-'], max_output=archive_limit).stdout
            if len(raw) > archive_limit:
                raise ValueError('Oversized result')
            with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
                entries = archive.getmembers()
                if len(entries) != 1 or not entries[0].isfile() or entries[0].size > result_limit:
                    raise ValueError('Invalid result artifact')
                captured['result'] = json.loads(scrub(archive.extractfile(entries[0]).read().decode(), secrets), object_pairs_hook=unique)
        except (RunnerError, ValueError, tarfile.TarError, UnicodeError):
            captured['missing'].append('valid result artifact')
        try:
            captured['logs'] = scrub(self.call(['logs', '--tail', '1000', current['id']], combine=True).stdout.decode(errors='replace'), secrets)[-65536:]
        except RunnerError:
            captured['missing'].append('runner logs')
        if job.get('task') is not None or job.get('delegation') is not None:
            from .delegation_artifacts import MAX_BUNDLE_SIZE
            try:
                raw = self.call(['cp', current['id'] + ':/work/output.bundle', '-'], max_output=MAX_BUNDLE_SIZE + 65536).stdout
                with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
                    entries = archive.getmembers()
                    if len(entries) != 1 or not entries[0].isfile() or not 0 < entries[0].size <= MAX_BUNDLE_SIZE:
                        raise ValueError('Invalid delegated output artifact')
                    captured['worktree_bundle'] = archive.extractfile(entries[0]).read()
            except (RunnerError, ValueError, tarfile.TarError):
                captured['missing'].append('delegated output bundle')
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
        token = self.job_secrets(job).get('github_token')
        # Installation tokens are ghs_…; anything else was never minted by GitHub.
        token_revocation = self.revoke(token) if isinstance(token, str) and token.startswith('ghs_') else 'not_available'
        path = self._secrets_path(job)
        if path:
            path.unlink(missing_ok=True)
        return {'container_absent': True, 'capacity_ready': True, 'auth_refresh': auth_refresh,
                'token_revocation': token_revocation}
