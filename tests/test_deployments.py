"""Deployment profiles: one read-only strategist at a time, proposals only."""
import json
import subprocess
import tarfile
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch

from forge.deployments import Deployments
from forge.factory.github import GitHub, GitHubUnavailable
from forge.factory.runner import ContainerRunner, RunnerCapacity


def result(job_id, **overrides):
    value = {'version': 1, 'attempt': job_id, 'role': 'strategist', 'outcome': 'pass', 'blocker': None,
             'summary': 'deploy.yaml pushes to production', 'strategy': 'header',
             'deploy': {'workflow': 'deploy.yaml', 'environment': 'production', 'trigger': 'push:main'},
             'domain': {'value': 'academy.xrpl-commons.org', 'source': 'workflow', 'confidence': 'high'},
             'exposure': {'kind': 'none', 'path': None, 'field': None, 'header': None},
             'commit_source': 'image tag sha-<commit>',
             'proposal': {'title': 'Expose the build commit', 'body': 'Add X-Build-Commit.'}}
    value.update(overrides)
    return value


class FakeGitHub:
    def __init__(self):
        self.digest, self.head = 'digest-one', 'a' * 40
        self.reads, self.tokens = [], []

    def workflows_digest(self, item):
        self.reads.append(item)
        return self.digest, self.head

    def token(self, role, repo_id, *, worker=False):
        self.tokens.append((role, repo_id, worker))
        return 'synthetic-worker-token-not-a-real-credential'


class FakeSettings:
    def __init__(self, rows):
        self.rows = rows

    def view(self):
        return {'repositories': [dict(row) for row in self.rows]}


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1000.0
        self.engine, self.results, self.resets, self.prepared = {}, {}, [], []
        self.capacity = None
        test = self

        class Runner:
            def __init__(self, config, **kw): self.config = config
            def ready(self): return True
            def capacity(self, job=None): return {'allowed': True, 'reason': 'capacity_available'}
            def inspect(self, job): return test.engine.get(job['id'])
            def create(self, job):
                if test.capacity:
                    raise test.capacity
                test.engine[job['id']] = {'id': job['id'], 'status': 'created', 'running': False,
                                          'exit_code': 0, 'oom_killed': False}
                return job['id']
            def prepare(self, job, token): test.prepared.append((job['id'], job['role'], token))
            def start(self, job):
                if test.capacity:
                    raise test.capacity
                test.engine[job['id']].update(status='running', running=True)
            def observe(self, job, current=None): return {'at': test.now, 'logs': 'running output', 'execution': current}
            def capture(self, job):
                return {'result': test.results.get(job['id']), 'logs': '', 'missing': [],
                        'execution': test.engine.get(job['id'])}
            def reset(self, job):
                assert job['fenced'], 'reset requires persisted fencing'
                test.resets.append(job['id'])
                test.engine.pop(job['id'], None)

        self.runner_class = Runner
        self.github = FakeGitHub()
        self.settings = FakeSettings([{'id': 7, 'name': 'example/site', 'enabled': True,
                                       'access_ready': True, 'dispatch_allowed': True}])
        self.config = {'image': 'sha256:' + 'a' * 64, 'memory': '4g', 'engine': '/usr/bin/docker',
                       'harness_argv': ['/usr/local/bin/python3', '-I', '/opt/forge/harness.py'],
                       'grace_seconds': 10, 'proxy': 'http://forge-egress:3128', 'provider': 'codex'}

    def deployments(self):
        return Deployments(self.root, runner=lambda config: self.runner_class(config),
                           github=self.github, settings=self.settings, clock=lambda: self.now)

    def entry(self, repo_id=7):
        return self.deployments().read()['repositories'][str(repo_id)]

    def second_repository(self):
        self.settings.rows.append({'id': 8, 'name': 'example/api', 'enabled': True,
                                   'access_ready': True, 'dispatch_allowed': True})

    def finish(self, repo_id=7, **overrides):
        """Let the running container exit with a result, as the engine would."""
        job = self.entry(repo_id)['job']['id']
        self.results[job] = result(job, **overrides)
        self.engine[job].update(status='exited', running=False, exit_code=0)
        return job

    def test_first_tick_starts_one_strategist_per_repository_and_records_its_proposal(self):
        deployments = self.deployments()
        deployments.tick(self.config)
        entry = self.entry()
        self.assertEqual(entry['status'], 'running')
        self.assertEqual(entry['job']['role'], 'strategist')
        self.assertEqual(entry['job']['slot'], 'deployment-strategist')
        self.assertEqual(entry['job']['publisher'], 'forge-strategist')
        self.assertEqual(entry['job']['head'], 'a' * 40)
        self.assertEqual(entry['job']['runner_config']['harness_argv'], self.config['harness_argv'])
        self.assertEqual(self.prepared, [(entry['job']['id'], 'strategist',
                                          'synthetic-worker-token-not-a-real-credential')])
        self.assertEqual(self.github.tokens, [('controller', 7, True)])
        job = self.finish()
        self.deployments().tick(self.config)
        entry = self.entry()
        self.assertEqual(entry['status'], 'proposed')
        self.assertEqual(entry['proposal']['domain']['value'], 'academy.xrpl-commons.org')
        self.assertEqual(entry['proposal']['strategy'], 'header')
        self.assertEqual(entry['summary'], 'deploy.yaml pushes to production')
        self.assertEqual(entry['workflows_digest'], 'digest-one')
        self.assertEqual(entry['head'], 'a' * 40)
        self.assertEqual(entry['observed_at'], self.now)
        self.assertIsNone(entry['error'])
        self.assertEqual(self.resets, [job])
        self.assertFalse(self.engine)
        # A proposal alone never repeats, and never becomes a profile by itself.
        self.now += 4000
        self.deployments().tick(self.config)
        self.assertFalse(self.engine)
        self.assertEqual(self.entry()['status'], 'proposed')

    def test_only_one_strategist_runs_at_a_time(self):
        self.second_repository()
        self.deployments().tick(self.config)
        self.assertEqual(len(self.engine), 1)
        first = self.entry(7)['job']['id']
        self.assertIsNone(self.deployments().read()['repositories'].get('8', {}).get('job'))
        self.finish(7)
        self.deployments().tick(self.config)
        self.assertEqual(self.entry(7)['status'], 'proposed')
        self.assertEqual(len(self.engine), 1)
        second = self.entry(8)['job']['id']
        self.assertNotEqual(first, second)
        self.assertEqual(self.entry(8)['status'], 'running')

    def test_workflows_change_reruns_and_reread_reruns(self):
        self.deployments().tick(self.config)
        self.finish()
        self.deployments().tick(self.config)
        self.assertEqual(self.entry()['status'], 'proposed')
        # Unchanged workflows, even long after the digest window, start nothing.
        self.now += 2000
        self.deployments().tick(self.config)
        self.assertFalse(self.engine)
        self.github.digest, self.github.head = 'digest-two', 'b' * 40
        self.now += 2000
        self.deployments().tick(self.config)
        self.assertEqual(self.entry()['status'], 'running')
        self.assertEqual(self.entry()['job']['head'], 'b' * 40)
        self.finish()
        self.deployments().tick(self.config)
        self.assertEqual(self.entry()['workflows_digest'], 'digest-two')
        # An operator re-read runs again inside the digest window.
        self.deployments().request_reread(7)
        self.assertTrue(self.entry()['reread_requested'])
        self.deployments().tick(self.config)
        self.assertEqual(self.entry()['status'], 'running')
        self.assertFalse(self.entry()['reread_requested'])

    def test_digest_is_read_at_most_every_thirty_minutes(self):
        self.deployments().tick(self.config)
        self.finish()
        self.deployments().tick(self.config)
        self.assertEqual(len(self.github.reads), 1)
        self.now += 900
        self.deployments().tick(self.config)
        self.assertEqual(len(self.github.reads), 1)
        self.now += 1000
        self.deployments().tick(self.config)
        self.assertEqual(len(self.github.reads), 2)

    def test_malformed_result_marks_failed_and_offers_reread(self):
        self.deployments().tick(self.config)
        job = self.entry()['job']['id']
        self.results[job] = {'version': 1, 'attempt': job, 'role': 'strategist', 'outcome': 'pass',
                             'summary': 'read the repository'}
        self.engine[job].update(status='exited', running=False, exit_code=0)
        self.deployments().tick(self.config)
        entry = self.entry()
        self.assertEqual(entry['status'], 'failed')
        self.assertIsNone(entry['proposal'])
        self.assertTrue(entry['summary'])
        self.assertEqual(entry['workflows_digest'], 'digest-one')
        self.assertEqual(self.resets, [job])
        # A failure does not loop; it waits for a re-read or a workflows change.
        self.now += 2000
        self.deployments().tick(self.config)
        self.assertFalse(self.engine)
        self.deployments().request_reread(7)
        self.deployments().tick(self.config)
        self.assertEqual(self.entry()['status'], 'running')

    def test_unable_outcome_and_timeout_record_the_reason(self):
        self.deployments().tick(self.config)
        self.finish(outcome='unable', summary='The repository has no deploy workflow')
        self.deployments().tick(self.config)
        self.assertEqual(self.entry()['status'], 'failed')
        self.assertEqual(self.entry()['summary'], 'The repository has no deploy workflow')
        self.github.digest = 'digest-two'
        self.now += 2000
        self.deployments().tick(self.config)
        self.assertEqual(self.entry()['status'], 'running')
        self.now = self.entry()['job']['deadline'] + 1
        self.deployments().tick(self.config)
        self.assertEqual(self.entry()['status'], 'failed')
        self.assertIn('deadline', self.entry()['summary'])
        self.assertFalse(self.engine)

    def test_confirm_returns_profile_with_operator_domain_and_reject_stops_reruns(self):
        self.second_repository()
        self.deployments().tick(self.config)
        self.finish(7)
        self.deployments().tick(self.config)
        self.finish(8)
        self.deployments().tick(self.config)
        profile = self.deployments().confirm(7, 'academy.example.org')
        self.assertEqual(profile['domain'], 'academy.example.org')
        self.assertEqual(profile['strategy'], 'header')
        self.assertEqual(profile['deploy']['environment'], 'production')
        self.assertEqual(profile['exposure']['kind'], 'none')
        self.assertEqual(profile['workflows_digest'], 'digest-one')
        self.assertIsNone(profile['exposure_issue'])
        self.assertEqual(self.entry(7)['status'], 'confirmed')
        with self.assertRaises(ValueError):
            self.deployments().confirm(8, 'https://api.example.org/health')
        self.deployments().reject(8, 'The domain is wrong')
        self.assertEqual(self.entry(8)['status'], 'rejected')
        self.assertEqual(self.entry(8)['reason'], 'The domain is wrong')
        # Neither a confirmation nor a rejection starts the strategist again.
        self.now += 4000
        self.deployments().tick(self.config)
        self.assertFalse(self.engine)
        self.assertEqual(self.entry(7)['status'], 'confirmed')
        self.assertEqual(self.entry(8)['status'], 'rejected')
        with self.assertRaises(ValueError):
            self.deployments().confirm(7, 'academy.example.org')

    def test_confirm_keeps_the_strategist_domain_when_the_operator_types_none(self):
        self.deployments().tick(self.config)
        self.finish()
        self.deployments().tick(self.config)
        self.assertEqual(self.deployments().confirm(7, '')['domain'], 'academy.xrpl-commons.org')

    def test_a_proposed_domain_is_a_bare_host_or_the_result_is_refused(self):
        self.deployments().tick(self.config)
        self.finish(domain={'value': 'https://academy.example.org/health', 'source': 'workflow', 'confidence': 'high'})
        self.deployments().tick(self.config)
        self.assertEqual(self.entry()['status'], 'failed')
        self.assertIsNone(self.entry()['proposal'])

    def test_capacity_wait_keeps_the_job_and_resumes(self):
        self.capacity = RunnerCapacity({'allowed': False, 'reason': 'memory_headroom'})
        self.deployments().tick(self.config)
        entry = self.entry()
        self.assertEqual(entry['status'], 'running')
        self.assertFalse(entry['job']['fenced'])
        self.assertIsNotNone(entry['job']['capacity_wait_since'])
        self.assertFalse(self.engine)
        self.capacity = None
        self.now += 60
        self.deployments().tick(self.config)
        entry = self.entry()
        self.assertIsNone(entry['job'].get('capacity_wait_since'))
        self.assertEqual(entry['job']['deadline'], self.now + Deployments.TIMEOUT)
        self.assertTrue(self.engine)

    def test_repositories_without_dispatch_access_are_never_read(self):
        self.settings.rows[0]['dispatch_allowed'] = False
        self.deployments().tick(self.config)
        self.assertFalse(self.github.reads)
        self.assertFalse(self.engine)
        self.assertFalse(self.deployments().read()['repositories'])

    def test_a_github_failure_is_recorded_on_the_repository_and_never_raised(self):
        self.github.workflows_digest = Mock(side_effect=GitHubUnavailable('GitHub observation is unconfirmed'))
        self.deployments().tick(self.config)
        self.assertEqual(self.entry()['error'], 'GitHub observation is unconfirmed')
        self.assertEqual(self.entry()['status'], 'idle')
        self.assertFalse(self.engine)


class ContractTests(unittest.TestCase):
    def test_prepare_writes_the_read_only_strategist_note(self):
        config = {'image': 'sha256:' + 'a' * 64, 'memory': '4g', 'engine': '/usr/bin/docker',
                  'engine_socket': '/var/run/docker.sock', 'provider_environment': ['OPENAI_API_KEY'],
                  'provider_auth_file_env': None, 'harness_argv': ['/usr/local/bin/python3'],
                  'grace_seconds': 10, 'proxy': 'http://forge-egress:3128', 'provider': 'codex', 'model': None}
        job = {'id': '00000000-0000-4000-8000-000000000001', 'role': 'strategist', 'repo': 'example/site',
               'repo_id': 7, 'publisher': 'forge-strategist', 'target_branch': None, 'branch': None,
               'candidate': None, 'issue_number': None, 'history': {}, 'head': 'c' * 40,
               'deadline': 100.0, 'fenced': False, 'slot': 'deployment-strategist'}
        run = Mock(return_value=subprocess.CompletedProcess([], 0, b'', b''))
        runner = ContainerRunner(config, run=run, environ={'OPENAI_API_KEY': 'synthetic-provider-value'})
        with patch.object(ContainerRunner, 'inspect', return_value={'id': 'abc', 'status': 'created'}):
            runner.prepare(job, 'synthetic-token-not-a-real-credential')
        with tarfile.open(fileobj=BytesIO(run.call_args.kwargs['input'])) as archive:
            note = json.loads(archive.extractfile('assignment.json').read())
        self.assertEqual(note['role'], 'strategist')
        self.assertEqual(note['head'], 'c' * 40)
        self.assertIsNone(note['candidate'])
        self.assertIn('Read only', note['instructions'])
        self.assertNotIn('issue_number', note)
        self.assertEqual(note['harness_argv'], config['harness_argv'])

    def github(self, contents):
        github = GitHub(Mock(), {}, client=Mock())
        calls = []

        def call(role, repo, method, suffix, payload=None):
            calls.append((role, method, suffix))
            if suffix == '':
                return {'id': 7, 'default_branch': 'main'}
            if suffix == '/branches/main':
                return {'name': 'main', 'commit': {'sha': 'd' * 40}}
            if suffix == '/contents/.github/workflows?ref=' + 'd' * 40:
                if contents is None:
                    raise GitHubUnavailable('Not found', status=404)
                return contents
            raise AssertionError(suffix)

        github.call = call
        return github, calls

    def test_workflows_digest_reads_the_default_branch_head_and_its_workflow_files(self):
        files = [{'path': '.github/workflows/deploy.yaml', 'sha': 'e' * 40, 'type': 'file'},
                 {'path': '.github/workflows/ci.yaml', 'sha': 'f' * 40, 'type': 'file'}]
        github, calls = self.github(files)
        value, head = github.workflows_digest({'repo': 'example/site', 'repo_id': 7})
        self.assertEqual(head, 'd' * 40)
        self.assertEqual(len(value), 64)
        self.assertEqual([c[2] for c in calls], ['', '/branches/main', '/contents/.github/workflows?ref=' + 'd' * 40])
        self.assertTrue(all(c[0] == 'controller' and c[1] == 'GET' for c in calls))
        # The digest follows the workflow contents, not the listing order.
        again, _ = self.github(list(reversed(files)))[0].workflows_digest({'repo': 'example/site', 'repo_id': 7})
        self.assertEqual(again, value)
        changed, _ = self.github([{**files[0], 'sha': '0' * 40}, files[1]])[0].workflows_digest({'repo': 'example/site', 'repo_id': 7})
        self.assertNotEqual(changed, value)

    def test_a_repository_without_workflows_has_a_stable_digest(self):
        empty, head = self.github(None)[0].workflows_digest({'repo': 'example/site', 'repo_id': 7})
        self.assertEqual(head, 'd' * 40)
        self.assertEqual(empty, self.github([])[0].workflows_digest({'repo': 'example/site', 'repo_id': 7})[0])

    def test_an_unverified_repository_identity_is_refused(self):
        github, _ = self.github([])
        with self.assertRaises(GitHubUnavailable):
            github.workflows_digest({'repo': 'example/site', 'repo_id': 9})


class ServiceHookTests(unittest.TestCase):
    """The supervisor cycle drives deployments; it never fails because of them."""

    def setUp(self):
        from forge.app_access import ROLE_PERMISSIONS
        from forge.onboarding import Onboarding, ROLES
        from forge.settings import write_json
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.api = Mock()
        self.api.jwt.return_value = 'synthetic-app-jwt'
        self.setup = Onboarding(self.root, client=self.api)
        self.setup.read()
        self.api.discover.side_effect = lambda connection: {
            'repositories': [{'id': 10, 'name': 'example/project'}],
            'permissions': ROLE_PERMISSIONS[ROLES[connection['app_id'] - 1]]}
        config = self.setup.settings().load()
        config['connections'] = {role: {'app_id': index, 'installation_id': 100 + index,
                                        'private_key_env': 'FORGE_SETUP_' + role.upper() + '_KEY'}
                                 for index, role in enumerate(ROLES, 1)}
        write_json(self.setup.settings().path, config)
        self.setup.settings().refresh()
        with self.setup.store.transaction() as data:
            data['apps'] = {role: {'connection': config['connections'][role]} for role in ROLES}
            data['owner'], data['account_type'] = 'example', 'organization'
        self.setup.select(10)
        self.setup.provider('test-provider-value-no-real-credential')
        runtime = self.setup.runtime(); runtime['image'] = 'sha256:' + 'a' * 64
        write_json(self.root / 'setup/runtime.json', runtime)

    def service(self):
        from forge.setup_service import Service
        engine = {}

        class Runner:
            def __init__(self, config, **kw): self.config = config
            def ready(self): return True
            def capacity(self, job=None): return {'allowed': True, 'reason': 'capacity_available'}
            def observe(self, job, current=None): return {'logs': '', 'at': 0}
            def inspect(self, job): return engine.get(job['id'])
            def create(self, job):
                engine[job['id']] = {'id': job['id'], 'status': 'created', 'running': False, 'exit_code': 0}
                return job['id']
            def prepare(self, job, token): pass
            def start(self, job):
                engine[job['id']].update(status='exited' if job.get('kind') == 'provider' else 'running',
                                         running=job.get('kind') != 'provider')
            def capture(self, job): return {'result': {'ready': True, 'attempt': job['id']}, 'logs': '', 'missing': []}
            def reset(self, job): engine.pop(job['id'], None)

        github = Mock()
        github.return_value.call.return_value = {'id': 10, 'default_branch': 'main'}
        github.return_value.protection_report.return_value = {'branch': 'main', 'available': True, 'requirements': []}
        service = Service(self.root, runner=Runner, github=github)
        service.setup = self.setup
        return service

    def activated(self):
        service = self.service()
        self.setup.queue('check', self.setup.view()['revision'])
        for _ in range(16):
            service.tick()
        self.setup.queue('start', self.setup.view()['revision'])
        service.tick()
        self.assertEqual(self.setup.read()['phase'], 'active')
        return service

    def cycle(self, service, **kwargs):
        from forge.settings import Settings
        with patch('forge.setup_service.Supervisor', **{'return_value.cycle.return_value': {'at': 0}}), \
                patch.object(Settings, 'start_refresh'), \
                patch('forge.setup_service.Deployments', **kwargs) as deployments:
            service.tick()
        return deployments

    def test_the_supervisor_cycle_ticks_deployments_with_the_runtime_configuration(self):
        service = self.activated()
        deployments = self.cycle(service)
        deployments.return_value.tick.assert_called_once()
        self.assertEqual(deployments.return_value.tick.call_args.args[0]['image'], 'sha256:' + 'a' * 64)
        self.assertEqual(deployments.call_args.args[0], self.root)

    def test_a_deployment_failure_is_recorded_and_never_breaks_the_cycle(self):
        from forge.factory.runner import RunnerError
        service = self.activated()
        deployments = self.cycle(service, **{'return_value.tick.side_effect': RunnerError('Engine unavailable')})
        deployments.return_value.fail.assert_called_once_with('Engine unavailable')
        service.next_cycle = 0
        deployments = self.cycle(service, **{'return_value.tick.side_effect': OSError('disk')})
        self.assertEqual(deployments.return_value.fail.call_count, 1)
        self.assertNotIn('disk', deployments.return_value.fail.call_args.args[0])

    def test_tick_is_skipped_while_a_setup_operation_is_pending(self):
        import uuid
        service = self.activated()
        with self.setup.store.transaction() as data:
            data['operation'] = {'id': str(uuid.uuid4()), 'action': 'harness_check', 'step': 'queued',
                                 'providers': ['pool-entry'], 'generation': data['generation'],
                                 'revision': 'synthetic-revision'}
        deployments = self.cycle(service)
        deployments.return_value.tick.assert_not_called()

    def test_a_recorded_failure_survives_in_the_store(self):
        Deployments(self.root, runner=Mock(), github=Mock(), settings=Mock()).fail('Engine unavailable')
        data = Deployments(self.root, runner=Mock(), github=Mock(), settings=Mock()).read()
        self.assertEqual(data['error'], 'Engine unavailable')
        self.assertEqual(data['repositories'], {})
        self.assertEqual(data['version'], 1)


if __name__ == '__main__':
    unittest.main()
