"""Stable supervisor entrypoint: setup checks, activation, the factory cycle, and reset."""
import argparse
from copy import deepcopy
import fcntl
import json
from pathlib import Path
import shutil
import signal
import subprocess
import time
import uuid

from .onboarding import Onboarding
from .repository_policy import protection_ready
from .credentials import Credentials
from .deployments import Deployments
from .settings import Settings, read_json, write_json
from .state import StateError
from .factory.config import load
from .factory.control import controller_lock, requests
from .factory.github import GitHub, GitHubUnavailable
from .factory.ledger import Ledger
from .factory.runner import ContainerRunner, RunnerError, RunnerCapacity
from .factory.supervisor import Supervisor

# How often a queued or running conversation is driven between cycles.
DISCUSSION_POLL_SECONDS = 4


PROBE_FAILURE_MESSAGES = {
    'provider_login_expired': 'The server login has expired. Run bin/forge setup login {provider} on the server',
    'runner_environment_unavailable': 'The runner build environment failed verification. Qualify the installed image before checking again',
    'provider_usage_limit': 'The {provider} account has reached its usage limit or has no credit left. Wait for the limit to reset or connect another account, then check again',
    'provider_connection_failed': 'The runner did not get a valid response from the {provider} provider. Check the egress proxy and the provider status, then retry',
    'native_sandbox_failed': 'The {provider} native tool sandbox failed verification inside the runner. Check the runner image and the host isolation policies',
}


def harness_error(logs):
    """Last normalized harness error event in bounded runner output; adapters emit fixed labels only."""
    for line in reversed(logs.splitlines()[-200:]):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get('type') == 'error' and isinstance(event.get('message'), str):
            return ''.join(c for c in event['message'] if ' ' <= c <= '~')[:160]
    return None


def probe_failure_message(capture, provider, *, timed_out=False):
    reason = (capture.get('result') or {}).get('reason')
    if reason in PROBE_FAILURE_MESSAGES:
        return PROBE_FAILURE_MESSAGES[reason].format(provider=provider)
    if timed_out:
        return 'The ' + provider + ' check did not finish before its deadline. Check the provider status and the egress proxy, then retry'
    message = 'Provider or native sandbox check failed. Check the server connection and runner installation, then retry'
    detail = harness_error(capture.get('logs') or '')
    if isinstance(reason, str) and reason:
        return message + ' (reason: ' + ''.join(c for c in reason if c.isalnum() or c in '_-')[:64] + ')'
    if detail:
        return message + ' (the harness reported: ' + detail + ')'
    return message


class Service:
    def __new__(cls, root, **kwargs):
        if cls is Service:
            from .store.runtime import selected
            if selected(root) is not None:
                from .sqlite_setup_service import SqliteService
                return object.__new__(SqliteService)
        return object.__new__(cls)

    def __init__(self, root, *, runner=ContainerRunner, github=GitHub):
        self.root = Path(root)
        self.setup = Onboarding(root)
        self.runner_class, self.github_class = runner, github
        self.next_cycle = 0
        self.next_sync = 0
        self.next_discussion = 0
        # Names this process: a discussion worker an earlier one started and
        # that is gone without an answer was orphaned by a restart.
        self.boot = uuid.uuid4().hex

    def runner(self, config):
        # The runner finds <root>/runner-seccomp.json through FORGE_CREDENTIAL_ROOT.
        return self.runner_class(config, environ=Credentials(self.root).environment())

    def update(self, expected, **values):
        with self.setup.store.transaction() as data:
            if (data['generation'] != expected['generation']
                    or (data.get('operation') or {}).get('id') != expected['id']):
                raise StateError('Setup operation was superseded')
            data.update(values)

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
        if not protection_ready(report):
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

    @staticmethod
    def draining(scope, config, records):
        """A rotating login has one writer, as in factory.providers.available.

        An unscoped check waits for every worker; a scoped one waits only while a
        worker still holds one of the checked logins. API-key accounts run in
        parallel, so they are never waited for.
        """
        if not scope:
            return bool(records['slots'])
        references = {entry['provider_auth_file_env'] for entry in config['provider_pool']
                      if entry['id'] in scope and entry.get('provider_auth_file_env')}
        return any(not job['cleanup'] and (job.get('runner_config') or {}).get('provider_auth_file_env') in references
                   for job in records['jobs'].values())

    def drain(self, scope):
        config = load(self.root)
        ledger = Ledger(self.root / 'state/factory')
        with controller_lock(self.root):
            supervisor = Supervisor(ledger, self.runner(config), self.github_class(Settings(self.root), config), Settings(self.root), config)
            for job in ledger.read()['jobs'].values():
                if not job['cleanup'] and job['role'] != 'merge':
                    supervisor.execution(job)
            return self.draining(scope, config, ledger.read())

    def runner_verified(self, op):
        """Worker isolation is a property of the image the last qualification covered."""
        return bool(op.get('providers')) and bool(op.get('image')) and op['image'] == self.setup.runtime()['image']

    def complete(self, data, op):
        config = self.setup.active_runtime() if data['phase'] == 'active' else self.setup.runtime()
        checks, scope = data.get('provider_checks', {}), op.get('providers')
        identifiers = [entry['id'] for entry in config['provider_pool']]
        passed = [key for key, value in checks.items() if value == 'passed']
        verified = bool(passed)
        if scope:
            # Only the checked accounts change; the others keep their verdict.
            ready = identifiers if config['provider_ready'] is None else [i for i in config['provider_ready'] if i in identifiers]
            passed = [i for i in ready if i not in scope] + [i for i in identifiers if i in scope and checks.get(i) == 'passed']
            verified = any(checks.get(i) == 'passed' for i in scope)
        if config['provider_pool']:
            config['provider_ready'] = passed
            self.setup.save_runtime(config, active=data['phase'] == 'active')
        self.update(op, probe_jobs=data['probe_jobs'], provider_checks=checks, provider_errors=data.get('provider_errors', {}),
                    probe_error=None, checks={**data['checks'], 'provider': 'passed' if verified else 'failed', 'runner': 'passed'},
                    qualification={'key': self.setup.qualification_key(data), 'at': self.setup.clock(),
                                   'image': self.setup.runtime()['image']} if verified else None,
                    readiness=None if op['action'] == 'harness_check' or not verified else {'revision': self.setup.revision(data), 'at': time.time()},
                    operation=None, error=None if verified else 'No coding account passed verification. Review the account and runner diagnostics before checking again.')

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
                job['cleanup'] = True
            if data['probe_jobs']:
                self.update(op, probe_jobs=data['probe_jobs'])
            scope = op.get('providers')
            if data['phase'] == 'active' and op['action'] == 'harness_check':
                # Drain normally before probing shared login credentials. A full
                # check blocks new dispatch while cleanup continues; a scoped one
                # waits only for the workers holding the checked login, and tick()
                # keeps dispatching the harnesses outside the scope.
                if self.drain(scope):
                    return
            config = self.setup.runtime() if op['action'] == 'harness_check' else self.access(data)
            self.runner(config).ready()
            if op['action'] == 'check' and self.setup.qualified(data):
                self.update(op, checks={'github': 'passed', 'provider': 'passed', 'runner': 'passed'},
                            readiness={'revision': self.setup.revision(data), 'at': self.setup.clock()}, operation=None, error=None)
                return
            pool = config['provider_pool'] or [{'id': config['provider']}]
            if scope:
                pool = [entry for entry in pool if entry['id'] in scope]
                if not pool:
                    raise ValueError('The checked coding account is no longer connected; check again')
            plan = [{**config, **{k: v for k, v in entry.items() if k != 'id'}} for entry in pool]
            job = self.new_probe(plan[0], 'provider')
            job['provider_id'] = pool[0]['id']
            self.update(op, probe_jobs=[job], probe_plan=plan, probe_ids=[e['id'] for e in pool],
                        provider_checks={k: v for k, v in (data.get('provider_checks') or {}).items() if k not in scope} if scope else {},
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
                elif self.runner_verified(op):
                    self.complete(data, op)
                    return
                else:
                    data['probe_jobs'].append(self.new_probe(self.setup.runtime(), 'termination'))
            elif job['kind'] == 'termination':
                data['probe_jobs'].append(self.new_probe(self.setup.runtime(), 'reuse'))
            else:
                self.complete(data, op)
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
            try:
                runner.start(job)
            except RunnerCapacity:
                job.setdefault('capacity_wait_since', time.time())
                self.update(op, probe_jobs=data['probe_jobs'])
                return
            if job.get('capacity_wait_since') is not None:
                job['deadline'] += max(0, time.time() - job.pop('capacity_wait_since'))
                self.update(op, probe_jobs=data['probe_jobs'])
            return
        error = None
        if job['kind'] == 'provider' and current['running'] and time.time() < job['deadline']:
            return
        if job['kind'] == 'provider':
            capture = runner.capture(job)
            job['capture'] = capture
            if current['running'] or current['exit_code'] != 0 or capture['result'] != {'ready': True, 'attempt': job['id']}:
                error = probe_failure_message(capture, job['runner_config']['provider'], timed_out=current['running'])
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
        with controller_lock(self.root):
            if op['step'] == 'queued':
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
                            operation={**op, 'step': 'stop_workers', 'jobs': jobs})
                return
            if op['step'] == 'stop_workers':
                from .harnesses.accounts import reset_logins
                reset_logins(self.root)
                for job in [*op['jobs'], *data['probe_jobs']]:
                    self.runner(job['runner_config']).reset(job)
                self.update(op, operation={**op, 'step': 'clear_setup'})
                return
            if op['step'] != 'clear_setup':
                raise ValueError('Unrecognized reset step')
            with Settings(self.root).locked('sync'), self.setup.settings().locked('sync'):
                archive = self.root / 'archives' / ('reset-' + op['id'])
                archive.parent.mkdir(mode=0o700, exist_ok=True)
                archive.mkdir(parents=True, mode=0o700, exist_ok=True)
                # Keep the journal and fences outside the archive. Never archive credential values.
                paths = [self.root / name for name in ('factory.json', 'runtime.json', 'setup')]
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
        from .factory.discussions import Discussions
        discussions = Discussions(self.root, runner=self.runner, boot=self.boot)
        if op:
            # Setup/reset must not race a discussion's rotating provider login.
            with controller_lock(self.root):
                discussions.cancel_all()
                jobs = discussions.active_jobs()
                if jobs:
                    discussions.drive(jobs[0], None)  # fenced: cleanup needs no GitHub credential
                    return
        scoped = bool(op and op['action'] == 'harness_check' and op.get('providers') and data['phase'] == 'active')
        if op:
            try:
                if op['action'] == 'reset':
                    self.reset(data)
                elif op['action'] == 'start':
                    self.start(data)
                else:
                    self.check(data)
            except RunnerCapacity:
                if data.get('probe_jobs'):
                    data['probe_jobs'][-1].setdefault('capacity_wait_since', time.time())
                    self.update(op, probe_jobs=data['probe_jobs'])
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
            if not scoped:
                return
            # A scoped check only holds back its own accounts. Every other
            # harness keeps dispatching while this login is verified.
        if (self.root / 'state/factory-paused.json').exists():
            marker = self.root / 'state/factory-onboarding.enabled'
            fence = read_json(self.root / 'state/factory-paused.json')
            if (data['phase'] == 'setup' and data.get('reset_receipt') and marker.exists()
                    and read_json(marker).get('generation') == data['generation']
                    and fence.get('generation') != data['generation']):
                (self.root / 'state/factory-paused.json').unlink()
                return
            # A crash between fencing and recording reset requires an explicit retry.
            return
        if data['phase'] != 'active':
            return
        if time.monotonic() < self.next_cycle:
            if not op:
                self.converse(discussions)
            return
        with controller_lock(self.root):
            settings, config = Settings(self.root), load(self.root)
            if scoped:
                identifiers = [entry['id'] for entry in config['provider_pool']]
                ready = identifiers if config['provider_ready'] is None else config['provider_ready']
                config['provider_ready'] = [i for i in ready if i in identifiers and i not in op['providers']]
            if time.monotonic() >= self.next_sync:
                settings.start_refresh()
                self.next_sync = time.monotonic() + settings.load()['sync_interval_seconds']
            ledger = Ledger(self.root / 'state/factory')
            requests(self.root, ledger)
            # Discussions clean up first; productive jobs get first choice of new capacity.
            github = self.github_class(settings, config)
            try:
                discussions.tick(config, github, settings, launch=False)
            except (RunnerError, GitHubUnavailable, ValueError, StateError, OSError):
                pass  # Durable intent/lease remains; cleanup is retried next cycle.
            observation = Supervisor(ledger, self.runner(config), self.github_class(settings, config), settings, config).cycle()
            write_json(self.root / 'state/factory/supervisor.json', observation)
            if not op:
                # Deployment tracking is an observation; it never holds up a cycle,
                # and it gets a quarter of the remaining observation budget.
                deployments = None
                try:
                    github = self.github_class(settings, config)
                    github.deadline = time.monotonic() + config['cycle_budget_seconds'] / 4
                    deployments = Deployments(self.root, runner=self.runner, github=github, settings=settings)
                    deployments.tick(config)
                except Exception as error:
                    message = str(error) if isinstance(error, (ValueError, RunnerError, StateError, GitHubUnavailable)) else \
                        'Deployment tracking failed (' + type(error).__name__ + '); it will be retried next cycle'
                    try:
                        (deployments or Deployments(self.root, runner=self.runner, github=None, settings=settings)).fail(message)
                    except Exception:
                        pass
            if not op:
                try:
                    github = self.github_class(settings, config)
                    github.deadline = time.monotonic() + 10
                    discussions.tick(config, github, settings)
                except (RunnerError, GitHubUnavailable, ValueError, StateError, OSError):
                    pass  # Durable discussion intent is retried; never corrupt deployment state.
            self.next_cycle = time.monotonic() + config['cycle_seconds']
            self.next_discussion = time.monotonic() + DISCUSSION_POLL_SECONDS

    def converse(self, discussions):
        """Between cycles, keep a live conversation moving.

        A cycle runs every cycle_seconds (30 s by default), so a question used to
        wait up to a cycle for its worker and a finished answer up to another
        before the operator saw it (measured: 16-54 s and about 25 s). Only
        discussions tick here; pipeline dispatch still waits for the cycle, and
        Discussions.tick itself defers to pipeline work waiting to launch.
        """
        if time.monotonic() < self.next_discussion or not discussions.live():
            return
        self.next_discussion = time.monotonic() + DISCUSSION_POLL_SECONDS
        try:
            with controller_lock(self.root):
                settings, config = Settings(self.root), load(self.root)
                github = self.github_class(settings, config)
                github.deadline = time.monotonic() + 10
                discussions.tick(config, github, settings)
        except (RunnerError, GitHubUnavailable, ValueError, StateError, OSError):
            pass  # The next cycle retries; durable intent stays in the thread.


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
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                    # The reason is the whole value of this line: a cycle that
                    # cannot run repeats every two seconds, and an operator
                    # reading the log has nothing else to go on.
                    print('Supervisor enquiry failed; ownership retained for retry: '
                          + type(error).__name__ + ': ' + str(error), flush=True)
                time.sleep(2)
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    # Runtime adapters subclass the canonical module's classes. Running the
    # local __main__ copy would skip initialization of that returned subclass.
    from forge.setup_service import main as canonical_main
    canonical_main()
