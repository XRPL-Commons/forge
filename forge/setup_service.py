"""Stable supervisor entrypoint: legacy operation, setup checks, activation, and reset."""
import argparse
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time
import uuid

from .onboarding import Onboarding
from .credentials import Credentials
from .settings import Settings, read_json, write_json
from .state import StateError
from .factory.config import load
from .factory.control import controller_lock, requests
from .factory.github import GitHub
from .factory.ledger import Ledger
from .factory.runner import ContainerRunner, RunnerError
from .factory.supervisor import Supervisor


def command(args):
    return subprocess.run(args, capture_output=True, text=True, check=True, timeout=30).stdout


def stop_auxiliary(root):
    """Retire only legacy services whose installed command belongs to this runtime."""
    if not (Path(root) / 'supervisor.sh').is_file():
        return
    for name in ('forge-triage', 'forge-activity', 'forge-observability', 'forge-notify', 'forge-observer'):
        result = subprocess.run(['systemctl', '--user', 'show', name + '.service', '--property=ExecStart', '--value'],
                                capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise ValueError('Could not verify legacy services before reset')
        if str(Path(root).resolve()) + '/' in result.stdout:
            command(['systemctl', '--user', 'disable', '--now', name + '.timer'])
            command(['systemctl', '--user', 'stop', name + '.service'])


def legacy_inventory(root):
    """Only configured terminals in individually identifiable user scopes are resettable."""
    from .config import load_repositories
    from .lifecycle import observe_generation
    names = {r.short + '-' + role for r in load_repositories(Path(root) / 'repos.d', include_factory=False)
             for role in ('build', 'review', 'audit')}
    if not names:
        return []
    result = subprocess.run(['tmux', 'list-panes', '-a', '-F', '#{session_name}|#{pane_pid}'],
                            capture_output=True, text=True, timeout=5)
    if result.returncode:
        # Explicit no-server is absence; permission/socket failures are unknown.
        if any(s in result.stderr for s in ('no server running', 'No such file or directory')):
            return []
        raise ValueError('Could not inventory legacy terminals; check the tmux server')
    panes = []
    for line in result.stdout.splitlines():
        name, pid = line.split('|')
        path = Path('/proc') / pid / 'cgroup'
        groups = [v[3:] for v in path.read_text().splitlines() if v.startswith('0::')]
        if len(groups) != 1:
            raise ValueError('Legacy terminal has no verifiable cgroup')
        panes.append((name, groups[0]))
    found = []
    for name, group in panes:
        if name not in names:
            continue
        scope = group.rsplit('/', 1)[-1]
        identity = observe_generation(name)
        if (not re.fullmatch(r'tmux-spawn-[a-f0-9-]+\.scope', scope) or not identity
                or any(n != name and g == group for n, g in panes)
                or '..' in Path(group).parts):
            raise ValueError('Legacy worker isolation is unverified; stop its dedicated scope before retrying reset')
        found.append({'name': name, 'generation': identity['id'], 'scope': scope, 'cgroup': group})
    return found


def reset_legacy(record):
    from .lifecycle import observe_generation
    current = observe_generation(record['name'])
    if current and current['id'] != record['generation']:
        raise ValueError('Legacy worker generation changed; reconcile it before continuing reset')
    group = Path('/sys/fs/cgroup') / record['cgroup'].lstrip('/')
    events = group / 'cgroup.events'
    if events.exists() and 'populated 1' in events.read_text():
        command(['systemctl', '--user', 'kill', '--kill-whom=all', '--signal=SIGKILL', record['scope']])
        return False
    if current:
        command(['tmux', 'kill-session', '-t', record['generation'].split('|')[0]])
        return False
    return not events.exists() or 'populated 0' in events.read_text()


class Service:
    def __init__(self, root, *, runner=ContainerRunner, github=GitHub):
        self.root = Path(root)
        self.setup = Onboarding(root)
        self.runner_class, self.github_class = runner, github
        self.legacy = None
        self.next_cycle = 0
        self.next_sync = 0

    def runner(self, config):
        env = Credentials(self.root).environment()
        policy = self.root / 'runner-seccomp.json'
        if policy.is_file():
            env.setdefault('FORGE_RUNNER_SECCOMP', str(policy))
        return self.runner_class(config, environ=env)

    def update(self, expected, **values):
        with self.setup.store.transaction() as data:
            if (data['generation'] != expected['generation']
                    or (data.get('operation') or {}).get('id') != expected['id']):
                raise StateError('Setup operation was superseded')
            data.update(values)

    def stop_legacy(self):
        if self.legacy:
            if self.legacy.poll() is None:
                os.killpg(self.legacy.pid, signal.SIGTERM)
                try:
                    self.legacy.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(self.legacy.pid, signal.SIGKILL)
                    self.legacy.wait(timeout=5)
            self.legacy = None

    def access(self, data):
        settings = self.setup.settings()
        settings.refresh()
        repo = data['selected']
        if not settings.admission(repo['name'])['allowed']:
            raise ValueError('Refresh all four GitHub App installations and grant access to the selected repository')
        config = self.setup.runtime()
        github = self.github_class(settings, config)
        item = {'repo': repo['name'], 'repo_id': repo['id']}
        metadata = github.call('controller', item, 'GET', '')
        if metadata.get('id') != repo['id'] or metadata.get('archived') or metadata.get('disabled'):
            raise ValueError('The selected repository is no longer available')
        report = github.protection_report(item, {'base_ref': metadata['default_branch']})
        if data.get('operation'):
            self.update(data['operation'], repository_check=report)
        missing = [row for row in report['requirements'] if not row['passed']]
        if not report['available'] or missing:
            raise ValueError('Repository protection needs attention on ' + metadata['default_branch'] + '. See the specific requirements below.')
        return config

    def new_probe(self, config, kind):
        config = deepcopy(config)
        config['harness_argv'] = (['/usr/local/bin/python3', '-I', '/opt/forge/probe.py'] if kind == 'provider' else
                                  ['/usr/local/bin/python3', '-c', 'import os,time; p=os.fork(); os.setsid() if p==0 else None; time.sleep(120)'])
        return {'id': str(uuid.uuid4()), 'slot': 'setup-probe', 'role': 'build', 'repo': 'example/setup',
                'repo_id': 1, 'issue_number': 1, 'publisher': 'setup-probe', 'target_branch': 'main',
                'branch': 'setup-probe', 'candidate': None, 'history': {}, 'deadline': time.time() + 180,
                'fenced': False, 'runner_config': config, 'kind': kind, 'cleanup': False}

    def check(self, data):
        op = data['operation']
        if op['step'] == 'queued':
            # Old probes are retained until verified absent, including interrupted checks.
            for job in data['probe_jobs']:
                job['fenced'] = True
            self.update(op, probe_jobs=data['probe_jobs'], checks={} if op['action'] == 'harness_check' else {'github': 'checking'}, probe_error=None, repository_check=None,
                        operation={**op, 'step': 'prepare'})
            return
        if op['step'] == 'prepare':
            for job in data['probe_jobs']:
                self.runner(job['runner_config']).reset(job)
            if data['phase'] == 'active' and op['action'] == 'harness_check':
                # Drain normally before probing shared login credentials. The
                # durable operation blocks new dispatch while cleanup continues.
                config = load(self.root)
                ledger = Ledger(self.root / 'state/factory')
                with controller_lock(self.root):
                    supervisor = Supervisor(ledger, self.runner(config), self.github_class(Settings(self.root), config), Settings(self.root), config)
                    for job in ledger.read()['jobs'].values():
                        if not job['cleanup'] and job['role'] != 'merge':
                            supervisor.execution(job)
                    if ledger.read()['slots']:
                        return
            config = self.setup.runtime() if op['action'] == 'harness_check' else self.access(data)
            self.runner(config).ready()
            if op['action'] == 'check' and self.setup.qualified(data):
                self.update(op, checks={'github': 'passed', 'provider': 'passed', 'runner': 'passed'},
                            readiness={'revision': self.setup.revision(data), 'at': self.setup.clock()}, operation=None, error=None)
                return
            pool = config['provider_pool'] or [{'id': config['provider']}]
            plan = [{**config, **{k: v for k, v in entry.items() if k != 'id'}} for entry in pool]
            job = self.new_probe(plan[0], 'provider')
            job['provider_id'] = pool[0]['id']
            self.update(op, probe_jobs=[job], probe_plan=plan, probe_ids=[e['id'] for e in pool], provider_checks={},
                        checks={**({} if op['action'] == 'harness_check' else {'github': 'passed'}), 'provider': 'checking', 'runner': 'checking'},
                        operation={**op, 'step': 'probe'})
            return
        job = data['probe_jobs'][-1]
        runner = self.runner(job['runner_config'])
        if job['fenced']:
            runner.reset(job)
            job['cleanup'] = True
            if data.get('probe_error') and job['kind'] != 'provider':
                self.update(op, probe_jobs=data['probe_jobs'], operation=None, error=data['probe_error'],
                            checks={**data['checks'], 'provider': 'failed'}, provider_checks={**data.get('provider_checks', {}), job.get('provider_id', 'runner'): 'failed'})
                return
            if job['kind'] == 'provider':
                data.setdefault('provider_checks', {})[job['provider_id']] = 'failed' if data.get('probe_error') else 'passed'
                data.setdefault('provider_errors', {})[job['provider_id']] = data.get('probe_error')
                index = len([j for j in data['probe_jobs'] if j['kind'] == 'provider'])
                if index < len(data['probe_plan']):
                    next_job = self.new_probe(data['probe_plan'][index], 'provider')
                    next_job['provider_id'] = data['probe_ids'][index]
                    data['probe_jobs'].append(next_job)
                else:
                    data['probe_jobs'].append(self.new_probe(self.setup.runtime(), 'termination'))
            elif job['kind'] == 'termination':
                data['probe_jobs'].append(self.new_probe(self.setup.runtime(), 'reuse'))
            else:
                config = load(self.root) if data['phase'] == 'active' else self.setup.runtime()
                passed = [key for key, value in data.get('provider_checks', {}).items() if value == 'passed']
                if config['provider_pool']:
                    config['provider_ready'] = passed
                    write_json(self.root / ('runtime.json' if data['phase'] == 'active' else 'setup/runtime.json'), config)
                self.update(op, probe_jobs=data['probe_jobs'], checks={**data['checks'], 'provider': 'passed' if passed else 'failed', 'runner': 'passed'},
                            qualification={'key': self.setup.qualification_key(data), 'at': self.setup.clock()} if passed else None,
                            readiness=None if op['action'] == 'harness_check' or not passed else {'revision': self.setup.revision(data), 'at': time.time()},
                            operation=None, error=None if passed else 'No harness passed verification. Reconnect a coding account on the VM and check again.')
                return
            self.update(op, probe_jobs=data['probe_jobs'], provider_checks=data.get('provider_checks', {}),
                        provider_errors=data.get('provider_errors', {}), probe_error=None)
            return
        current = runner.inspect(job)
        if current is None:
            job['container_id'] = runner.create(job)
            self.update(op, probe_jobs=data['probe_jobs'])
            current = runner.inspect(job)
        if current['status'] == 'created':
            runner.prepare(job, 'synthetic-no-github-access')
            runner.start(job)
            return
        error = None
        if job['kind'] == 'provider' and current['running'] and time.time() < job['deadline']:
            return
        if job['kind'] == 'provider':
            capture = runner.capture(job)
            if current['running'] or current['exit_code'] != 0 or capture['result'] != {'ready': True, 'attempt': job['id']}:
                error = ('The server login has expired. Run bin/forge setup login ' + job['runner_config']['provider'] + ' on the server'
                         if (capture.get('result') or {}).get('reason') == 'provider_login_expired' else
                         'Provider or native sandbox check failed. Check the server connection and runner installation, then retry')
        elif not current['running']:
            error = 'The runner stopped before the termination check; check the runner installation'
        elif job['kind'] == 'reuse':
            runner.reset(data['probe_jobs'][-2])
            if not runner.inspect(job)['running']:
                error = 'An old reset affected a replacement runner; check the runner installation'
        job['fenced'] = True
        self.update(op, probe_jobs=data['probe_jobs'], probe_error=error)

    def start(self, data):
        op = data['operation']
        if self.setup.revision(data) != op['revision']:
            raise ValueError('Setup changed; run readiness checks again')
        if not self.setup.qualified(data):
            raise ValueError('Coding account verification expired. Verify setup again before starting.')
        config = self.access(data)
        self.runner(config).ready()
        if (self.root / 'state/factory-paused.json').exists():
            raise ValueError('Reset is in progress; finish it before starting')
        with controller_lock(self.root):
            # The mode marker is committed last. Interrupted publication is replayable.
            if self.setup.revision(self.setup.read()) != op['revision']:
                raise ValueError('Setup changed during verification; check again before starting')
            config.update(enabled=True, merge_enabled=True)
            write_json(self.root / 'factory.json', self.setup.settings().load())
            write_json(self.root / 'runtime.json', config)
            write_json(self.root / 'state/github-access/access.json', self.setup.settings().observation())
            Ledger(self.root / 'state/factory').read()
            write_json(self.root / 'state/factory-onboarding.enabled', {'generation': data['generation']})
            write_json(self.root / 'state/factory-v2.enabled', {'generation': data['generation'], 'activated_at': time.time()})
            self.update(op, phase='active', operation=None, error=None,
                        activation_request=op['id'], activation_revision=op['revision'])

    def reset(self, data):
        op = data['operation']
        current = self.setup.read()
        if current['generation'] != op['generation'] or (current.get('operation') or {}).get('id') != op['id']:
            raise StateError('Reset belongs to an earlier setup generation')
        self.stop_legacy()
        with controller_lock(self.root):
            if op['step'] == 'queued':
                stop_auxiliary(self.root)
                legacy = legacy_inventory(self.root)
                jobs = []
                path = self.root / 'state/factory/factory-ledger.json'
                if path.exists() or path.with_suffix('.initialized').exists():
                    ledger = Ledger(path.parent)
                    with ledger.transaction() as records:
                        for job in records['jobs'].values():
                            job['fenced'] = True
                            if not job['cleanup'] and job['role'] != 'merge':
                                jobs.append(deepcopy(job))
                for job in data['probe_jobs']:
                    job['fenced'] = True
                self.update(op, probe_jobs=data['probe_jobs'],
                            operation={**op, 'step': 'stop_workers', 'legacy': legacy, 'jobs': jobs})
                return
            if op['step'] == 'stop_workers':
                from .harnesses.accounts import reset_logins
                reset_logins(self.root)
                for job in [*op['jobs'], *data['probe_jobs']]:
                    self.runner(job['runner_config']).reset(job)
                if not all([reset_legacy(record) for record in op['legacy']]):
                    return
                if legacy_inventory(self.root):
                    raise ValueError('A legacy worker appeared during reset; reconcile ownership before continuing')
                self.update(op, operation={**op, 'step': 'clear_setup'})
                return
            if op['step'] != 'clear_setup':
                raise ValueError('Unrecognized reset step')
            with Settings(self.root).locked('sync'), self.setup.settings().locked('sync'):
                archive = self.root / 'archives' / ('reset-' + op['id'])
                archive.parent.mkdir(mode=0o700, exist_ok=True)
                archive.mkdir(parents=True, mode=0o700, exist_ok=True)
                # Keep the journal and fences outside the archive. Never archive credential values.
                paths = [self.root / name for name in ('factory.json', 'runtime.json', 'repos.d', 'setup')]
                paths += [p for p in (self.root / 'state').iterdir()
                          if p.name not in {'onboarding', 'factory-paused.json', 'factory-onboarding.enabled'}]
                for source in paths:
                    if not source.exists():
                        continue
                    if source.is_symlink():
                        raise ValueError('Reset refuses a symlink in active configuration')
                    target = archive / source.relative_to(self.root)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists():
                        raise ValueError('Archive destination already exists; reconcile interrupted cleanup')
                    source.rename(target)
                credentials = self.root / 'credentials'
                if credentials.is_symlink():
                    raise ValueError('Reset refuses a symlink credential directory')
                if credentials.exists():
                    shutil.rmtree(credentials)
                generation = str(uuid.uuid4())
                write_json(self.root / 'state/factory-onboarding.enabled', {'generation': generation})
                write_json(archive / 'receipt.json', {'operation': op['id'], 'generation': data['generation'],
                                                   'at': time.time(), 'resources_verified_absent': True})
                self.update(op, generation=generation, phase='setup', operation=None, apps={}, checks={}, repository_check=None, probe_jobs=[],
                            selected=None, provider_revision=None, provider_checks={}, provider_errors={}, probe_plan=[], probe_ids=[], readiness=None, error=None, owner='', account_type=None,
                            challenge=None, activation_request=None, activation_revision=None,
                            reset_receipt={'id': op['id'], 'generation': op['generation'], 'at': time.time()})
                (self.root / 'state/factory-paused.json').unlink(missing_ok=True)

    def tick(self):
        write_json(self.root / 'state/setup-service.json', {'at': self.setup.clock()})
        data = self.setup.read()
        op = data.get('operation')
        if op:
            try:
                if op['action'] == 'reset':
                    self.reset(data)
                elif op['action'] == 'start':
                    self.start(data)
                else:
                    self.check(data)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                # No provider output/credentials in errors. Reset stays pending until cleanup succeeds.
                message = str(error) if isinstance(error, (ValueError, RunnerError, StateError)) else 'Operation failed; inspect the supervisor service and retry'
                try:
                    if op['action'] in ('check', 'harness_check') and any(not j['cleanup'] for j in data['probe_jobs']):
                        for job in data['probe_jobs']:
                            job['fenced'] = True
                        self.update(op, error=message, probe_error=message, probe_jobs=data['probe_jobs'])
                    else:
                        finished = {'operation': None} if op['action'] != 'reset' else {}
                        if op['action'] in ('check', 'harness_check'):
                            finished['checks'] = {key: 'failed' if value == 'checking' else value
                                                  for key, value in data['checks'].items()}
                        self.update(op, error=message, **finished)
                except StateError:
                    pass  # Reset superseded this check/activation.
            return
        if (self.root / 'state/factory-paused.json').exists():
            marker = self.root / 'state/factory-onboarding.enabled'
            fence = read_json(self.root / 'state/factory-paused.json')
            if (data['phase'] == 'setup' and data.get('reset_receipt') and marker.exists()
                    and read_json(marker).get('generation') == data['generation']
                    and fence.get('generation') != data['generation']):
                (self.root / 'state/factory-paused.json').unlink()
                return
            # A crash between fencing and recording reset requires an explicit retry.
            self.stop_legacy()
            return
        if data['phase'] == 'legacy':
            if self.legacy is None or self.legacy.poll() is not None:
                self.legacy = subprocess.Popen(['/bin/bash', str(self.root / 'supervisor.sh')], start_new_session=True)
            return
        self.stop_legacy()
        if data['phase'] != 'active' or time.monotonic() < self.next_cycle:
            return
        with controller_lock(self.root):
            settings, config = Settings(self.root), load(self.root)
            if time.monotonic() >= self.next_sync:
                settings.start_refresh()
                self.next_sync = time.monotonic() + settings.load()['sync_interval_seconds']
            ledger = Ledger(self.root / 'state/factory')
            requests(self.root, ledger)
            observation = Supervisor(ledger, self.runner(config), self.github_class(settings, config), settings, config).cycle()
            write_json(self.root / 'state/factory/supervisor.json', observation)
            self.next_cycle = time.monotonic() + config['cycle_seconds']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    service = Service(args.root)
    with open(Path(args.root) / 'state-setup-service.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def stop(*_):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, stop)
        try:
            while True:
                try:
                    service.tick()
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                    print('Supervisor enquiry failed; ownership retained for retry', flush=True)
                time.sleep(2)
        except KeyboardInterrupt:
            pass
        finally:
            service.stop_legacy()


if __name__ == '__main__':
    main()
