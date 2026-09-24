"""Deployment profiles: one read-only strategist at a time, proposals only."""
import json
import subprocess
import tarfile
import time
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
    def __init__(self, rows, roles=None):
        self.rows, self.roles = rows, roles or {}

    def view(self):
        return {'repositories': [dict(row) for row in self.rows], 'roles': self.roles}


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

    def test_a_result_that_matches_the_worker_schema_is_accepted(self):
        # Every role writes headline and plain; a strategist result that
        # satisfies harness.schema() must not be rejected for carrying them.
        self.deployments().tick(self.config)
        self.finish(headline='Deploys with GitHub Actions',
                    plain='The workflow pushes to production on every merge. Forge can read the commit from the site.')
        self.deployments().tick(self.config)
        entry = self.entry()
        self.assertEqual(entry['status'], 'proposed')
        self.assertEqual(entry['proposal']['strategy'], 'header')

    def test_a_proposed_path_that_could_change_the_host_is_refused(self):
        # 'https://' + domain + path: a path that does not start with a slash
        # would append to the host, and the card never shows it for a header.
        self.deployments().tick(self.config)
        self.finish(exposure={'kind': 'header', 'path': '.attacker.example/', 'field': None, 'header': 'X-Build-Commit'})
        self.deployments().tick(self.config)
        entry = self.entry()
        self.assertEqual(entry['status'], 'failed')
        self.assertIn('where the running commit is exposed', entry['summary'])

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
        # The recovery role is the controller App's read-only worker credential.
        self.assertEqual(self.github.tokens, [('recovery', 7, True)])
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

    def test_an_emptied_domain_box_means_github_deployments_only(self):
        # The card prefills the strategist's candidate, so an empty box is the
        # operator clearing it; only an absent value keeps the candidate.
        self.deployments().tick(self.config)
        self.finish()
        self.deployments().tick(self.config)
        self.assertIsNone(self.deployments().confirm(7, '')['domain'])

    def test_confirm_keeps_the_strategist_domain_when_no_domain_is_supplied(self):
        self.deployments().tick(self.config)
        self.finish()
        self.deployments().tick(self.config)
        self.assertEqual(self.deployments().confirm(7, None)['domain'], 'academy.xrpl-commons.org')

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

    def ledger_job(self, reference):
        """A pipeline worker holding the rotating coding account."""
        from forge.factory.ledger import Ledger
        ledger = Ledger(self.root / 'state/factory')
        ledger.discover({'id': 7, 'name': 'example/site'}, 3, 'requirements-digest')
        job = ledger.reserve('7:3', 'build', 'build-1', publisher='forge-build[bot]', policy='policy-digest',
                             timeout=600, runner_config={'provider_auth_file_env': reference})
        return ledger, job

    def test_a_rotating_coding_account_is_never_shared_with_a_pipeline_worker(self):
        self.config['provider_auth_file_env'] = 'FORGE_HARNESS_AUTH_ONE'
        ledger, job = self.ledger_job('FORGE_HARNESS_AUTH_ONE')
        self.deployments().tick(self.config)
        self.assertFalse(self.engine)
        self.assertFalse(self.github.reads)
        self.assertFalse(self.deployments().read()['repositories'])
        # An API-key job holds no lease, and a released worker gives its own back.
        ledger.job_update(job['id'], fenced=True)
        ledger.release(job['id'])
        self.deployments().tick(self.config)
        self.assertEqual(self.entry()['status'], 'running')

    def test_an_api_key_configuration_needs_no_lease(self):
        self.config['provider_auth_file_env'] = None
        self.ledger_job('FORGE_HARNESS_AUTH_ONE')
        self.deployments().tick(self.config)
        self.assertEqual(self.entry()['status'], 'running')

    def test_the_digest_loop_stops_at_the_cycle_observation_budget(self):
        import time as clock
        self.second_repository()
        self.github.deadline = clock.monotonic() - 1
        self.deployments().tick(self.config)
        self.assertFalse(self.github.reads)
        self.assertFalse(self.engine)
        self.github.deadline = clock.monotonic() + 30
        self.deployments().tick(self.config)
        self.assertEqual(len(self.github.reads), 1)

    def test_a_capacity_wait_does_not_outlive_the_deadline(self):
        self.capacity = RunnerCapacity({'allowed': False, 'reason': 'memory_headroom'})
        self.deployments().tick(self.config)
        self.assertEqual(self.entry()['status'], 'running')
        self.now = self.entry()['job']['deadline'] + 1
        self.deployments().tick(self.config)
        entry = self.entry()
        self.assertEqual(entry['status'], 'failed')
        self.assertIn('capacity', entry['summary'])
        self.assertTrue(entry['job']['cleanup'])
        self.assertEqual(self.resets, [entry['job']['id']])
        self.assertFalse(self.engine)

    def test_reading_the_store_never_writes_it(self):
        self.deployments().tick(self.config)
        path = self.root / 'state/deployments.json'
        before = (path.stat().st_mtime_ns, path.read_bytes())
        for _ in range(3):
            self.deployments().read()
        self.assertEqual((path.stat().st_mtime_ns, path.read_bytes()), before)

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


class ObservationContractTests(unittest.TestCase):
    """The deployment reads verify identity and ordering rather than trusting them."""

    def setUp(self):
        from forge.factory.github import COMPARISONS
        COMPARISONS.clear()
        self.addCleanup(COMPARISONS.clear)
        self.item = {'repo': 'example/site', 'repo_id': 7}

    def observer(self, responses):
        github = GitHub(Mock(), {}, client=Mock())
        calls = []

        def call(role, repo, method, suffix, payload=None, *, extra=None):
            calls.append((role, method, suffix, extra))
            value = responses[suffix]
            if isinstance(value, Exception):
                raise value
            return value

        github.call = call
        return github, calls

    def test_deployments_are_read_newest_first_on_the_recommended_grant(self):
        from forge.app_access import OPTIONAL_PERMISSIONS
        rows = [{'id': 4, 'sha': 'a' * 40}, {'id': 9, 'sha': 'b' * 40}, {'id': 6, 'sha': 'c' * 40}]
        github, calls = self.observer({'/deployments?per_page=5&environment=production': rows})
        self.assertEqual([row['id'] for row in github.deployments(self.item, 'production')], [9, 6, 4])
        self.assertEqual(calls, [('controller', 'GET', '/deployments?per_page=5&environment=production',
                                  OPTIONAL_PERMISSIONS['controller'])])

    def test_a_malformed_deployment_inventory_is_refused(self):
        for rows in ({'id': 1}, [{'id': 1}], [{'id': '1', 'sha': 'a' * 40}], [{'id': 1, 'sha': 'nope'}],
                     [{'id': True, 'sha': 'a' * 40}], ['deployment']):
            github, _ = self.observer({'/deployments?per_page=5': rows})
            with self.assertRaises(GitHubUnavailable):
                github.deployments(self.item, None)

    def test_the_latest_status_is_the_newest_identifier_not_the_first_row(self):
        rows = [{'id': 2, 'state': 'failure'}, {'id': 7, 'state': 'success'}]
        github, _ = self.observer({'/deployments/9/statuses?per_page=5': rows})
        self.assertEqual([row['id'] for row in github.deployment_statuses(self.item, 9)], [7, 2])
        for malformed in ([{'id': 1}], [{'state': 'success'}], [{'id': '1', 'state': 'success'}], {'state': 'success'}):
            github, _ = self.observer({'/deployments/9/statuses?per_page=5': malformed})
            with self.assertRaises(GitHubUnavailable):
                github.deployment_statuses(self.item, 9)

    def test_ancestry_is_asked_once_per_pair_and_only_trusts_known_answers(self):
        suffix = '/compare/' + 'a' * 40 + '...' + 'b' * 40
        for status, expected in (('ahead', True), ('identical', True), ('behind', False), ('diverged', False)):
            from forge.factory.github import COMPARISONS
            COMPARISONS.clear()
            github, calls = self.observer({suffix: {'status': status}})
            self.assertIs(github.contains(self.item, 'a' * 40, 'b' * 40), expected)
            self.assertIs(github.contains(self.item, 'a' * 40, 'b' * 40), expected)
            self.assertEqual(len(calls), 1)  # Ancestry between two commits never changes.
            self.assertIsNone(calls[0][3])   # Comparison needs no optional grant.
        COMPARISONS.clear()
        github, _ = self.observer({suffix: {'status': 'unknown'}})
        with self.assertRaises(GitHubUnavailable):
            github.contains(self.item, 'a' * 40, 'b' * 40)


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
        self.assertLess(deployments.call_args.kwargs['github'].deadline, time.monotonic() + 30)
        self.assertGreater(deployments.call_args.kwargs['github'].deadline, time.monotonic())

    def test_a_deployment_failure_is_recorded_and_never_breaks_the_cycle(self):
        from forge.factory.runner import RunnerError
        service = self.activated()
        deployments = self.cycle(service, **{'return_value.tick.side_effect': RunnerError('Engine unavailable')})
        deployments.return_value.fail.assert_called_once_with('Engine unavailable')
        service.next_cycle = 0
        deployments = self.cycle(service, **{'return_value.tick.side_effect': OSError('disk')})
        self.assertEqual(deployments.return_value.fail.call_count, 1)
        self.assertNotIn('disk', deployments.return_value.fail.call_args.args[0])
        service.next_cycle = 0
        deployments = self.cycle(service, **{'return_value.tick.side_effect': KeyError('repositories')})
        self.assertIn('KeyError', deployments.return_value.fail.call_args.args[0])
        service.next_cycle = 0
        self.cycle(service, side_effect=TypeError('constructor'))  # A construction failure is swallowed too.
        self.assertIsNone(self.setup.view()['error'])

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


PROFILE = {'strategy': 'health',
           'deploy': {'workflow': 'deploy.yaml', 'environment': 'production', 'trigger': 'push:main'},
           'domain': 'site.example',
           'exposure': {'kind': 'health', 'path': '/health', 'field': 'commit', 'header': None},
           'workflows_digest': 'digest-one', 'confirmed_at': 900, 'exposure_issue': None}
MERGE = 'b' * 40
LIVE = 'c' * 40


def copy_profile():
    from copy import deepcopy
    return deepcopy(PROFILE)


class ObservationGitHub:
    """Only the four reads observation is allowed to make."""

    def __init__(self):
        self.rows, self.statuses, self.compared, self.tokens = [], {}, [], []
        self.pulls, self.ancestors, self.token_error, self.error = {}, set(), None, None

    def token(self, role, repo_id, *, worker=False, extra=None):
        self.tokens.append((role, repo_id, worker, extra))
        if self.token_error and extra:
            raise self.token_error
        return 'synthetic-observation-token-not-a-real-credential'

    def deployments(self, item, environment, limit=5):
        if self.error:
            raise self.error
        return [dict(row) for row in self.rows if row.get('environment', environment) == environment][:limit]

    def deployment_statuses(self, item, deployment_id):
        return [dict(row) for row in self.statuses.get(deployment_id, [])]

    def contains(self, item, ancestor, descendant):
        self.compared.append((item['repo_id'], ancestor, descendant))
        return (ancestor, descendant) in self.ancestors

    def pr(self, item, number):
        return dict(self.pulls[number])


class ObservationTests(unittest.TestCase):
    """A verdict comes from a commit Forge read itself or from a deployment status."""

    def setUp(self):
        from forge.factory.ledger import Ledger
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1000.0
        self.github = ObservationGitHub()
        self.ledger = Ledger(self.root / 'state/factory', clock=lambda: self.now)
        self.settings = FakeSettings([{'id': 7, 'name': 'example/site', 'enabled': True,
                                       'access_ready': True, 'dispatch_allowed': True,
                                       'deployment': {'status': 'confirmed', 'profile': copy_profile()}}])
        self.live, self.live_error, self.reads = None, None, []

    def reader(self, url, *, kind, field=None, header=None):
        self.reads.append((url, kind, field, header))
        if self.live_error:
            raise self.live_error
        return self.live

    def deployments(self):
        return Deployments(self.root, runner=None, github=self.github,
                           settings=self.settings, clock=lambda: self.now)

    def observe(self):
        return self.deployments().observe(self.github, self.ledger, self.reader)

    def profile(self, **fields):
        self.settings.rows[0]['deployment']['profile'].update(fields)

    def merged(self, number=3, *, merge=MERGE, pr=2):
        key = self.ledger.discover({'id': 7, 'name': 'example/site'}, number, 'requirements')
        with self.ledger.transaction() as data:
            candidate = {'pr': pr, 'head': 'a' * 40}
            if merge:
                candidate['merge_commit'] = merge
            data['issues'][key].update(state='merged', candidate=candidate)
        return key

    def issue(self, key):
        return self.ledger.read()['issues'][key]

    def entry(self, repo_id=7):
        return self.deployments().read()['repositories'][str(repo_id)]

    def deployment(self, sha, state, *, identifier=5, run_url='https://github.com/example/site/runs/1'):
        self.github.rows = [{'id': identifier, 'sha': sha, 'environment': 'production', 'created_at': '2026-09-24T10:00:00Z'}]
        self.github.statuses = {identifier: [{'state': state, 'created_at': '2026-09-24T10:05:00Z', 'log_url': run_url}]}

    def test_a_live_commit_equal_to_the_merge_marks_the_issue_deployed(self):
        key = self.merged()
        self.live = MERGE
        self.assertEqual(self.observe(), [])
        self.assertEqual(self.issue(key)['deployed'], {'commit': MERGE, 'at': self.now, 'via': 'live'})
        self.assertEqual(self.reads, [('https://site.example/health', 'health', 'commit', None)])
        self.assertEqual(self.entry()['observed']['live'], {'commit': MERGE, 'at': self.now})
        self.assertFalse(self.github.compared)
        self.assertIn('deployed', [e['kind'] for e in self.ledger.read()['events']])
        # A verdict is recorded once; a later cycle neither rewrites nor re-announces it.
        self.now += 60
        self.observe()
        self.assertEqual(self.issue(key)['deployed']['at'], 1000.0)
        self.assertEqual(sum(e['kind'] == 'deployed' for e in self.ledger.read()['events']), 1)

    def test_a_short_live_commit_still_matches_its_merge(self):
        key = self.merged()
        self.live = MERGE[:7]
        self.observe()
        self.assertEqual(self.issue(key)['deployed']['via'], 'live')
        self.assertFalse(self.github.compared)

    def test_a_live_commit_ahead_of_the_merge_marks_the_issue_deployed(self):
        key = self.merged()
        self.live = LIVE
        self.github.ancestors.add((MERGE, LIVE))
        self.observe()
        self.assertEqual(self.github.compared, [(7, MERGE, LIVE)])
        self.assertEqual(self.issue(key)['deployed'], {'commit': LIVE, 'at': self.now, 'via': 'live'})

    def test_a_live_commit_without_the_merge_leaves_the_issue_waiting(self):
        key = self.merged()
        self.live = LIVE
        self.assertEqual(self.observe(), [])
        self.assertIsNone(self.issue(key).get('deployed'))
        self.assertIsNone(self.issue(key).get('deploy_failed'))

    def test_a_successful_deployment_containing_the_merge_marks_it_deployed(self):
        key = self.merged()
        self.profile(exposure={'kind': 'none', 'path': None, 'field': None, 'header': None}, domain=None)
        self.deployment(LIVE, 'success')
        self.github.ancestors.add((MERGE, LIVE))
        self.assertEqual(self.observe(), [])
        self.assertFalse(self.reads)
        self.assertEqual(self.issue(key)['deployed'], {'commit': LIVE, 'at': self.now, 'via': 'github'})
        self.assertEqual(self.entry()['observed']['deploy'],
                         {'sha': LIVE, 'state': 'success', 'at': '2026-09-24T10:05:00Z',
                          'run_url': 'https://github.com/example/site/runs/1'})
        from forge.app_access import OPTIONAL_PERMISSIONS
        self.assertIn(('controller', 7, False, OPTIONAL_PERMISSIONS['controller']), self.github.tokens)

    def test_a_pending_deployment_is_not_a_verdict(self):
        key = self.merged()
        self.deployment(LIVE, 'in_progress')
        self.github.ancestors.add((MERGE, LIVE))
        self.observe()
        self.assertIsNone(self.issue(key).get('deployed'))
        self.assertIsNone(self.issue(key).get('deploy_failed'))

    def test_a_failed_deployment_is_recorded_until_a_later_success(self):
        key = self.merged()
        self.deployment(LIVE, 'failure')
        self.github.ancestors.add((MERGE, LIVE))
        self.observe()
        self.assertEqual(self.issue(key)['deploy_failed'],
                         {'sha': LIVE, 'at': self.now, 'run_url': 'https://github.com/example/site/runs/1'})
        self.assertIsNone(self.issue(key).get('deployed'))
        self.now += 60
        self.deployment(LIVE, 'success', identifier=6)
        self.observe()
        self.assertEqual(self.issue(key)['deployed']['via'], 'github')
        self.assertIsNone(self.issue(key)['deploy_failed'])

    def test_a_deployment_that_predates_the_merge_says_nothing_about_it(self):
        key = self.merged()
        self.deployment('d' * 40, 'failure')
        self.observe()
        self.assertIsNone(self.issue(key).get('deploy_failed'))
        self.assertEqual(self.github.compared, [(7, MERGE, 'd' * 40)])
        self.github.rows = []
        self.observe()
        self.assertIsNone(self.entry()['observed']['deploy'])

    def test_a_domain_failure_is_reported_and_retried_with_a_backoff(self):
        from forge.factory.live import LiveUnavailable
        key = self.merged()
        self.live_error = LiveUnavailable('The deployment did not answer in time')
        for _ in range(3):
            self.assertEqual(self.observe(), [{'repo': 'example/site',
                                               'reason': 'The deployment did not answer in time'}])
            self.now += 60
        self.assertEqual(len(self.reads), 3)
        self.assertIsNone(self.issue(key).get('deployed'))
        self.assertEqual(self.entry()['observed']['error'], 'The deployment did not answer in time')
        # Three consecutive failures earn a ten-minute pause, not a read every cycle,
        # and the pause never makes a failing domain look healthy.
        self.now += 60
        self.assertEqual(self.observe(), [{'repo': 'example/site',
                                           'reason': 'The deployment did not answer in time (retrying in 8 min)'}])
        self.assertEqual(len(self.reads), 3)
        self.assertEqual(self.entry()['observed']['error'],
                         'The deployment did not answer in time (retrying in 8 min)')
        self.now += 600
        self.live_error, self.live = None, MERGE
        self.observe()
        self.assertEqual(len(self.reads), 4)
        self.assertEqual(self.issue(key)['deployed']['via'], 'live')
        self.assertIsNone(self.entry()['observed']['error'])

    def test_a_repository_without_a_confirmed_profile_is_never_observed(self):
        key = self.merged()
        self.settings.rows[0]['deployment'] = {'status': 'proposed', 'profile': None}
        self.assertEqual(self.observe(), [])
        self.assertFalse(self.reads)
        self.assertFalse(self.github.tokens)
        self.assertFalse(self.deployments().read()['repositories'])
        self.assertIsNone(self.issue(key).get('deployed'))

    def test_a_disabled_repository_is_never_observed(self):
        self.merged()
        self.settings.rows[0]['enabled'] = False
        self.assertEqual(self.observe(), [])
        self.assertFalse(self.reads)

    def test_a_refused_deployments_token_never_stops_the_domain_read(self):
        key = self.merged()
        self.settings.roles = {'controller': {'missing_recommended': ['deployments: read']}}
        self.github.token_error = GitHubUnavailable('Role credential could not be issued for this repository')
        self.live = MERGE
        errors = self.observe()
        self.assertEqual(errors, [{'repo': 'example/site',
                                   'reason': 'GitHub deployments need the recommended permission'}])
        self.assertTrue(self.reads)
        self.assertEqual(self.issue(key)['deployed']['via'], 'live')

    def test_a_credential_failure_is_never_reported_as_a_missing_permission(self):
        self.merged()
        self.settings.roles = {'controller': {'missing_recommended': []}}
        self.github.token_error = GitHubUnavailable('Installation mapping needs a fresh access sync')
        self.assertEqual(self.observe(), [{'repo': 'example/site',
                                           'reason': 'Installation mapping needs a fresh access sync'}])
        self.settings.roles = {'controller': {'missing_recommended': ['checks: write']}}
        self.assertEqual(self.observe()[0]['reason'], 'Installation mapping needs a fresh access sync')

    def test_an_unreadable_deployments_api_is_recorded_per_repository(self):
        self.merged()
        self.github.error = GitHubUnavailable('GitHub observation or publication is unconfirmed')
        self.observe()
        self.assertEqual(self.entry()['observed']['error'], 'GitHub observation or publication is unconfirmed')
        self.assertTrue(self.reads)

    def test_the_merge_commit_is_read_from_the_pull_request_once(self):
        key = self.merged(merge=None)
        self.github.pulls[2] = {'number': 2, 'merged': True, 'merge_commit_sha': MERGE}
        self.live = MERGE
        self.observe()
        self.assertEqual(self.issue(key)['candidate']['merge_commit'], MERGE)
        self.github.pulls.clear()  # A second cycle must not need the PR again.
        self.now += 60
        self.observe()
        self.assertEqual(self.issue(key)['deployed']['via'], 'live')

    def test_an_unmerged_pull_request_yields_no_verdict(self):
        key = self.merged(merge=None)
        self.github.pulls[2] = {'number': 2, 'merged': False, 'merge_commit_sha': None}
        self.live = MERGE
        self.observe()
        self.assertIsNone(self.issue(key).get('deployed'))
        self.assertIsNone(self.issue(key)['candidate'].get('merge_commit'))

    def test_only_merged_issues_are_ever_marked_deployed(self):
        key = self.ledger.discover({'id': 7, 'name': 'example/site'}, 4, 'requirements')
        self.assertFalse(self.ledger.deployed(key, {'commit': MERGE, 'via': 'live'}))
        self.assertFalse(self.ledger.deploy_failed(key, {'sha': MERGE, 'run_url': None}))
        self.assertIsNone(self.issue(key).get('deployed'))

    def test_another_repository_merge_is_never_judged_by_this_domain(self):
        key = self.ledger.discover({'id': 8, 'name': 'example/api'}, 9, 'requirements')
        with self.ledger.transaction() as data:
            data['issues'][key].update(state='merged', candidate={'pr': 4, 'merge_commit': MERGE})
        self.live = MERGE
        self.observe()
        self.assertIsNone(self.issue(key).get('deployed'))

    def test_the_observation_stops_at_the_cycle_budget(self):
        import time as clock
        self.merged()
        self.github.deadline = clock.monotonic() - 1
        self.assertEqual(self.observe(), [])
        self.assertFalse(self.reads)

    def test_the_supervisor_records_deployment_errors_without_failing_its_cycle(self):
        from forge.factory.config import validate
        from forge.factory.ledger import Ledger
        from forge.factory.live import LiveUnavailable
        from forge.factory.supervisor import Supervisor
        self.settings.root = self.root
        self.settings.admission = lambda repo: {'allowed': True}
        self.live_error = LiveUnavailable('The deployment did not answer in time')
        config = validate({'enabled': False, 'image': 'sha256:' + 'a' * 64, 'harness_argv': ['/adapter']})
        supervisor = Supervisor(Ledger(self.root / 'state/factory'), None, self.github,
                                self.settings, config, clock=lambda: self.now)
        with patch('forge.factory.live.read_commit', self.reader):
            observation = supervisor.cycle()
        self.assertEqual(observation['errors'], [{'repo': 'example/site', 'phase': 'deployment',
                                                  'reason': 'The deployment did not answer in time'}])
        self.assertTrue(self.reads)

    def test_the_factory_view_carries_the_deployment_verdicts(self):
        from forge.factory.control import deployments as view_deployments
        key = self.merged()
        self.live = MERGE
        self.observe()
        rows = view_deployments(self.root)
        self.assertEqual(rows, {'7': {'name': 'example/site', 'live': {'commit': MERGE, 'at': self.now},
                                      'deploy': None, 'error': None}})
        (self.root / 'state/deployments.json').write_text('{"repositories": ["broken"]}')
        self.assertEqual(view_deployments(self.root), {})
        (self.root / 'state/deployments.json').write_text('not json')
        self.assertEqual(view_deployments(self.root), {})


if __name__ == '__main__':
    unittest.main()
