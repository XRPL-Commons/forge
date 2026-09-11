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

from .contracts import assignment_note, identifier, unique


class RunnerError(RuntimeError):
    pass


class RunnerUnavailable(RunnerError):
    pass


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
            return {'id': value['Id'], 'status': value['State']['Status'],
                    'exit_code': value['State']['ExitCode'], 'running': value['State']['Running']}
        except (ValueError, KeyError, TypeError, IndexError):
            raise RunnerError('Container ownership or observation is unverified') from None

    def ready(self):
        self.call(['info', '--format', '{{.ServerVersion}}'])
        self.call(['image', 'inspect', self.config['image']])
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
        if config.get('apparmor_profile'):
            args[1:1] = ['--security-opt', 'apparmor=' + config['apparmor_profile']]
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
            auth_path = Path(self.environ.get(auth_ref, ''))
            if not auth_path.is_absolute() or not auth_path.is_file() or auth_path.stat().st_size > 65536:
                raise RunnerUnavailable('Provider authentication file is unavailable')
            try:
                credentials['FORGE_CODEX_AUTH'] = json.loads(auth_path.read_text())
            except (OSError, ValueError):
                raise RunnerUnavailable('Provider authentication file could not be loaded') from None
        credentials['GH_TOKEN'] = token
        note = assignment_note(job)
        note.update(harness_argv=self.config['harness_argv'], grace_seconds=self.config['grace_seconds'],
                    proxy=self.config['proxy'])
        content = io.BytesIO()
        with tarfile.open(fileobj=content, mode='w') as archive:
            for name, value in [('assignment.json', note), ('credentials.json', credentials)]:
                raw = json.dumps(value).encode()
                member = tarfile.TarInfo(name)
                member.size, member.mode, member.uid, member.gid = len(raw), 0o600, 1000, 1000
                archive.addfile(member, io.BytesIO(raw))
        self.call(['cp', '-a', '-', current['id'] + ':/work/'], input=content.getvalue())

    def start(self, job):
        if job['fenced']:
            raise RunnerError('Execution authority was revoked')
        current = self.inspect(job)
        if not current or current['status'] != 'created':
            raise RunnerError('Start requires the exact unstarted container')
        self.call(['start', current['id']])

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
        if current:
            if current['running']:
                # An unsuccessful graceful stop still goes through forced teardown.
                try:
                    self.call(['stop', '--time', str(self.config['grace_seconds']), current['id']],
                              timeout=self.config['grace_seconds'] + 15, check=False)
                except RunnerError:
                    pass  # Force removal still runs after a hung graceful stop.
            self.call(['rm', '--force', '--volumes', current['id']], timeout=30)
        if self.inspect(job) is not None:
            raise RunnerError('Teardown remains unverified')
        self.call(['info', '--format', '{{.ServerVersion}}'])
        return {'container_absent': True, 'capacity_ready': True}
