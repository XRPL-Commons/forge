"""Deployment profiles: one read-only strategist at a time, proposals only.

Every test runs against a selected SQLite store: profiles, strategist executions
and deployment verdicts are rows, seeded and inspected with SQL.
"""
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
from forge.factory.runner import ContainerRunner, RunnerCapacity, RunnerUnavailable
from forge.store.db import Store
from forge.store.provider_selection import select_provider


def selected_store(root):
    """A fresh store selected for this runtime root, as an installed Forge has."""
    store = Store.create(root)
    with store.write() as db:
        db.executemany('INSERT INTO store_metadata VALUES(?,?)', [('runtime_root', str(root)), ('runtime_active', 'true')])
    marker = Path(root) / 'state/storage'
    marker.write_text('forge-sqlite-v1\n' + str(store.root) + '\n' + store.instance_id + '\n')
    marker.chmod(0o600)
    return store


def unit(db, repo_id, number, *, name='example/site', state='building', at=90):
    """One issue in one unit, with the minimum rows the schema demands."""
    unit_id, issue, revision = 'unit-%d-%d' % (repo_id, number), '%d:%d' % (repo_id, number), 'revision-%d-%d' % (repo_id, number)
    db.execute('INSERT OR IGNORE INTO repositories(id,full_name) VALUES(?,?)', (repo_id, name))
    db.execute("INSERT INTO units(id,repository_id,base_branch,state,created_at,updated_at) VALUES(?,?,'main','building',?,?)",
               (unit_id, repo_id, at, at))
    db.execute("INSERT INTO issues(id,repository_id,github_number,github_state,current_revision_id,observed_at) VALUES(?,?,?,'open',?,?)",
               (issue, repo_id, number, revision, at))
    db.execute("INSERT INTO issue_revisions(id,issue_id,title,body,content_hash,requirements_hash,requirements_json,observed_at) "
               "VALUES(?,?,'Issue','Body','body','requirements','{}',?)", (revision, issue, at))
    db.execute("INSERT INTO unit_issues(id,unit_id,issue_id,repository_id,base_branch,requirements_revision_id,state,joined_at) "
               "VALUES(?,?,?,?,'main',?,'pending',?)", ('member-' + unit_id, unit_id, issue, repo_id, revision, at))
    if state != 'building':
        db.execute('UPDATE units SET state=? WHERE id=?', (state, unit_id))
    return unit_id


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


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = selected_store(self.root)
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
            def prepare(self, job):
                test.prepared.append((job['id'], job['role']))
                if getattr(test, 'prepare_error', None): raise test.prepare_error
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
        self.config = {'enabled': True, 'image': 'sha256:' + 'a' * 64, 'memory': '4g', 'engine': '/usr/bin/docker',
                       'harness_argv': ['/usr/local/bin/python3', '-I', '/opt/forge/harness.py'],
                       'grace_seconds': 10, 'proxy': 'http://forge-egress:3128', 'provider': 'codex'}

    def deployments(self):
        return Deployments(self.root, runner=lambda config: self.runner_class(config),
                           github=self.github, settings=self.settings, clock=lambda: self.now)

    def entry(self, repo_id=7):
        return self.deployments().read()['repositories'][str(repo_id)]

    def execution(self, identifier):
        with self.database.read_snapshot() as db:
            row = db.execute('SELECT domain,owner_id,phase,fenced,cleanup FROM auxiliary_executions WHERE id=?', (identifier,)).fetchone()
            return dict(row) if row else None

    def second_repository(self):
        self.settings.rows.append({'id': 8, 'name': 'example/api', 'enabled': True,
                                   'access_ready': True, 'dispatch_allowed': True})

    def finish(self, repo_id=7, **overrides):
        """Let the running container exit with a result, as the engine would."""
        job = self.entry(repo_id)['job']['id']
        self.results[job] = result(job, **overrides)
        self.engine[job].update(status='exited', running=False, exit_code=0)
        return job

    def available_account(self, config):
        with self.database.read_snapshot() as db:
            return select_provider(db, config, self.now)[0]

    def browser_pool(self):
        return {**self.config, 'provider_environment': ['OPENAI_API_KEY'], 'provider_auth_file_env': None,
            'provider_ready': ['codex-browser'], 'provider_pool': [
                {'id': 'unverified', 'provider': 'codex', 'model': 'ignored',
                 'provider_environment': ['OPENAI_API_KEY'], 'provider_auth_file_env': None},
                # Leasing is exclusive for a refreshable non-Codex login.
                {'id': 'codex-browser', 'provider': 'claude', 'model': 'selected-model',
                 'provider_environment': [], 'provider_auth_file_env': 'FORGE_SETUP_CODEX_AUTH'}]}

    def test_deployment_pins_verified_browser_pool_account_instead_of_default_api_key(self):
        config = self.browser_pool()
        self.deployments().tick(config)
        job = self.entry()['job']
        self.assertEqual(job['provider_id'], 'codex-browser')
        self.assertEqual(job['runner_config']['provider_environment'], [])
        self.assertEqual(job['runner_config']['provider_auth_file_env'], 'FORGE_SETUP_CODEX_AUTH')
        self.assertEqual(job['runner_config']['model'], 'selected-model')
        self.assertEqual(config['provider_environment'], ['OPENAI_API_KEY'])

    def test_unverified_or_leased_pool_account_never_creates_deployment_work(self):
        config = self.browser_pool()
        self.deployments().tick({**config, 'provider_ready': []})
        self.assertFalse(self.github.reads)
        held = self.pipeline_job('FORGE_SETUP_CODEX_AUTH')
        self.deployments().tick(config)
        self.assertFalse(self.github.reads)
        self.assertFalse(self.engine)
        self.release(held)
        self.deployments().tick(config)
        self.assertEqual(self.entry()['job']['provider_id'], 'codex-browser')

    def test_pool_account_is_rechecked_when_reserving_after_github_read(self):
        config = self.browser_pool()
        original = self.github.workflows_digest
        def concurrent_lease(item):
            result = original(item)
            self.pipeline_job('FORGE_SETUP_CODEX_AUTH')
            return result
        self.github.workflows_digest = concurrent_lease
        self.deployments().tick(config)
        with self.database.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM auxiliary_leases').fetchone()[0], 0)
        self.assertFalse(self.engine)
        self.assertFalse(self.deployments().read()['repositories'])
        self.github.workflows_digest = original
        with self.database.read_snapshot() as db:
            held = db.execute('SELECT job_id FROM job_leases').fetchone()[0]
        self.release(held)
        self.deployments().tick(config)
        self.assertEqual(self.entry()['status'], 'running')

    def test_real_source_enrichment_never_replaces_launch_manifest(self):
        source = Mock()
        source.mirror.prepared.return_value = {'source_input': 'immutable-input', 'execution_fence': 'physical-fence',
                                              'publication_authorization': {'kind': 'none'}}
        real = ContainerRunner(self.config, source=source)
        originals = {name: getattr(self.runner_class, name) for name in ('create', 'prepare', 'start', 'reset')}
        enriched = []
        def wrapped(name):
            def execute(runner, job):
                real.source_record(job)
                enriched.append(name)
                self.assertEqual(job['execution_fence'], 'physical-fence')
                return originals[name](runner, job)
            return execute
        with patch.object(self.runner_class, 'create', wrapped('create')), \
             patch.object(self.runner_class, 'prepare', wrapped('prepare')), \
             patch.object(self.runner_class, 'start', wrapped('start')), \
             patch.object(self.runner_class, 'reset', wrapped('reset')):
            deployments = self.deployments()
            deployments.tick(self.config)
            job = self.entry()['job']
            self.assertIsNotNone(job['container_id'])
            with self.database.read_snapshot() as db:
                manifest = db.execute('SELECT manifest_json FROM auxiliary_runtime WHERE execution_id=?', (job['id'],)).fetchone()[0]
            self.assertNotIn('source_input', json.loads(manifest))
            self.assertNotIn('execution_fence', json.loads(manifest))
            self.finish()
            deployments.tick(self.config)
        self.assertEqual(enriched, ['create', 'prepare', 'start', 'reset'])
        self.assertEqual(self.entry()['status'], 'proposed')
        self.assertTrue(self.execution(job['id'])['cleanup'])
        with self.database.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT manifest_json FROM auxiliary_runtime WHERE execution_id=?', (job['id'],)).fetchone()[0], manifest)
            self.assertEqual(db.execute('SELECT count(*) FROM auxiliary_leases').fetchone()[0], 0)

    def test_existing_fenced_capture_survives_source_enrichment_during_cleanup_retry(self):
        deployments = self.deployments()
        deployments.tick(self.config)
        job = self.entry()['job']; self.finish()
        job.update(fenced=True, capture=self.runner_class(self.config).capture(job))
        deployments.save(7, job=job)
        source = Mock()
        source.mirror.prepared.return_value = {'source_input': 'input', 'execution_fence': 'fence',
                                              'publication_authorization': {'kind': 'none'}}
        real = ContainerRunner(self.config, source=source)
        original = self.runner_class.reset
        def reset(runner, execution):
            real.source_record(execution)
            return original(runner, execution)
        with patch.object(self.runner_class, 'reset', reset):
            deployments.drive('7')
        self.assertEqual(self.entry()['status'], 'proposed')
        self.assertTrue(self.execution(job['id'])['cleanup'])
        with self.database.read_snapshot() as db:
            from forge.store.auxiliary import job_in
            self.assertEqual(job_in(db, job['id'])['capture'], job['capture'])

    def test_unavailable_preparation_cannot_hold_its_reservation_past_deadline(self):
        self.prepare_error = RunnerUnavailable('private credential detail')
        deployments = self.deployments()
        deployments.tick(self.config)
        job = self.entry()['job']; identifier = job['id']
        self.assertEqual(self.engine[identifier]['status'], 'created')
        self.assertNotIn('private credential', self.entry()['error'])
        deployments.tick(self.browser_pool())
        self.assertEqual(self.entry()['job']['runner_config'], job['runner_config'])
        prepared = len(self.prepared)
        self.now = job['deadline'] + 1
        # The expired assignment must never receive credentials or start again.
        self.prepare_error = AssertionError('expired prepare')
        deployments.tick({**self.config, 'enabled': False})
        self.assertEqual(len(self.prepared), prepared)
        self.assertIsNone(self.entry()['job'])
        self.assertIn('deadline', self.entry()['summary'])
        self.assertEqual(self.resets, [identifier])
        self.assertEqual(self.execution(identifier)['cleanup'], 1)
        with self.database.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM auxiliary_leases').fetchone()[0], 0)
            frozen = json.loads(db.execute('SELECT manifest_json FROM auxiliary_runtime WHERE execution_id=?', (identifier,)).fetchone()[0])
        self.assertEqual(frozen['runner_config'], job['runner_config'])

    def test_expired_unavailable_assignment_retries_cleanup_without_preparing(self):
        self.prepare_error = RunnerUnavailable('unavailable')
        deployments = self.deployments()
        deployments.tick(self.config)
        job = self.entry()['job']
        self.now = job['deadline'] + 1
        with patch.object(self.runner_class, 'reset', side_effect=OSError('engine unavailable')):
            with self.assertRaises(OSError): deployments.tick({**self.config, 'enabled': False})
        self.assertTrue(self.entry()['job']['fenced'])
        self.assertTrue(self.entry()['job']['capture']['timed_out'])
        self.prepare_error = AssertionError('fenced assignment prepared again')
        deployments.tick({**self.config, 'enabled': False})
        self.assertIsNone(self.entry()['job'])
        self.assertEqual(self.resets, [job['id']])

    def test_already_exited_result_can_be_collected_after_deadline(self):
        self.deployments().tick(self.config)
        self.finish()
        self.now = self.entry()['job']['deadline'] + 1
        self.deployments().tick({**self.config, 'enabled': False})
        self.assertEqual(self.entry()['status'], 'proposed')

    def test_the_selected_store_serves_the_deployment_profiles(self):
        # Store-independent callers construct Deployments(root, ...) and get the
        # selected store's implementation; no JSON state file is ever written.
        self.deployments().tick(self.config)
        self.assertTrue(hasattr(self.deployments(), 'database'))
        self.assertFalse((self.root / 'state/deployments.json').exists())
        with self.database.read_snapshot() as db:
            row = db.execute('SELECT status,execution_id FROM deployment_profiles WHERE repository_id=7').fetchone()
        self.assertEqual(row['status'], 'running')
        self.assertEqual(row['execution_id'], self.entry()['job']['id'])
        self.assertEqual(self.execution(row['execution_id'])['domain'], 'deployment')
        self.assertEqual(self.execution(row['execution_id'])['owner_id'], '7')

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
        self.assertEqual(self.prepared, [(entry['job']['id'], 'strategist')])
        # The recovery role is the controller App's read-only worker credential.
        self.assertEqual(self.github.tokens, [])
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
        # The finished execution is cleaned and gives its slot back.
        self.assertEqual(self.execution(job)['phase'], 'cleaned')
        with self.database.read_snapshot() as db:
            self.assertFalse(db.execute('SELECT 1 FROM auxiliary_leases WHERE execution_id=?', (job,)).fetchone())
        # A proposal alone never repeats, and never becomes a profile by itself.
        self.now += 4000
        self.deployments().tick(self.config)
        self.assertFalse(self.engine)
        self.assertEqual(self.entry()['status'], 'proposed')

    def test_disabled_factory_finishes_owned_worker_without_starting_replacement(self):
        self.second_repository()
        self.deployments().tick(self.config)
        identifier = self.finish(7)
        self.deployments().tick({**self.config, 'enabled': False})
        self.assertEqual(self.entry(7)['status'], 'proposed')
        self.assertEqual(self.resets, [identifier])
        self.assertFalse(self.engine)
        self.assertNotIn('8', self.deployments().read()['repositories'])
        self.deployments().tick({**self.config, 'enabled': False})
        self.assertFalse(self.engine)

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

    def test_a_running_observation_is_recorded_until_the_worker_exits(self):
        self.deployments().tick(self.config)
        self.assertIsNone(self.entry()['observation'])
        self.now += 30
        self.deployments().tick(self.config)
        self.assertEqual(self.entry()['observation']['logs'], 'running output')
        self.assertEqual(self.entry()['observation']['at'], self.now)
        self.finish()
        self.deployments().tick(self.config)
        self.assertIsNone(self.entry()['observation'])

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
        self.assertEqual(profile['confirmed_at'], self.now)
        self.assertIsNone(profile['exposure_issue'])
        self.assertEqual(self.entry(7)['status'], 'confirmed')
        with self.assertRaises(ValueError):
            self.deployments().confirm(8, 'https://api.example.org/health')
        self.assertEqual(self.entry(8)['status'], 'proposed')  # A refused domain changes nothing.
        with self.assertRaises(ValueError):
            self.deployments().reject(8, '   ')
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
        with self.assertRaises(ValueError):
            self.deployments().reject(7, 'Too late')

    def test_nothing_can_be_confirmed_or_rejected_without_a_proposal(self):
        with self.assertRaises(ValueError):
            self.deployments().confirm(7, None)
        with self.assertRaises(ValueError):
            self.deployments().reject(7, 'No proposal yet')
        self.deployments().tick(self.config)
        with self.assertRaises(ValueError):
            self.deployments().confirm(7, None)  # Still running.

    def test_a_failed_read_can_be_rejected(self):
        self.deployments().tick(self.config)
        self.finish(outcome='unable', summary='The repository has no deploy workflow')
        self.deployments().tick(self.config)
        self.deployments().reject(7, '  Not deployed anywhere  ')
        self.assertEqual(self.entry()['status'], 'rejected')
        self.assertEqual(self.entry()['reason'], 'Not deployed anywhere')

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

    def test_a_malformed_workflows_listing_is_recorded_without_its_detail(self):
        self.github.workflows_digest = Mock(side_effect=ValueError('unexpected listing shape'))
        self.deployments().tick(self.config)
        self.assertEqual(self.entry()['error'], 'The repository workflows could not be read')
        self.assertFalse(self.engine)

    def pipeline_job(self, reference, number=3, provider='claude'):
        """A pipeline worker holding a capacity lease with this refreshable coding account."""
        with self.database.write() as db:
            unit_id = unit(db, 7, number, state='building')
            db.execute("INSERT INTO attempts(id,unit_id,membership_generation,assignment_generation,input_manifest_json,budget_generation,created_at) "
                       "VALUES(?,?,1,1,'{}',1,100)", ('attempt-' + unit_id, unit_id))
            job = 'job-' + unit_id
            db.execute("INSERT INTO jobs(id,unit_id,attempt_id,role,phase,created_at) VALUES(?,?,?,'build','reserved',100)",
                       (job, unit_id, 'attempt-' + unit_id))
            db.execute('INSERT INTO job_leases VALUES(?,?,?,100)', ('build-' + unit_id, job, 'fence-' + job))
            db.execute("INSERT INTO job_launches(job_id,slot,execution_fence,branch,publisher,provider,runner_config_json,policy_json,context_json,deadline) "
                       "VALUES(?,?,?,'forge/work','forge-build[bot]',?,?,'{}','{}',700)",
                       (job, 'build-' + unit_id, 'fence-' + job, provider, json.dumps({'provider_auth_file_env': reference})))
        return job

    def release(self, job):
        """A pipeline worker is fenced and cleaned before its lease is released."""
        with self.database.write() as db:
            db.execute("UPDATE jobs SET fenced=1,phase='cleaned',cleanup=1 WHERE id=?", (job,))
            db.execute('DELETE FROM job_leases WHERE job_id=?', (job,))

    def test_a_rotating_coding_account_is_never_shared_with_a_pipeline_worker(self):
        self.config.update(provider='claude', provider_auth_file_env='FORGE_HARNESS_AUTH_ONE')
        job = self.pipeline_job('FORGE_HARNESS_AUTH_ONE')
        self.deployments().tick(self.config)
        self.assertFalse(self.engine)
        self.assertFalse(self.github.reads)
        self.assertFalse(self.deployments().read()['repositories'])
        # A released worker gives its own lease back.
        self.release(job)
        self.deployments().tick(self.config)
        self.assertEqual(self.entry()['status'], 'running')

    def test_another_coding_account_is_not_held_by_the_pipeline_worker(self):
        self.config['provider_auth_file_env'] = 'FORGE_HARNESS_AUTH_TWO'
        self.pipeline_job('FORGE_HARNESS_AUTH_ONE')
        self.deployments().tick(self.config)
        self.assertEqual(self.entry()['status'], 'running')

    def test_codex_access_snapshots_share_an_account_with_a_pipeline_worker(self):
        self.config['provider_auth_file_env'] = 'FORGE_HARNESS_AUTH_ONE'
        self.pipeline_job('FORGE_HARNESS_AUTH_ONE', provider='codex')
        self.deployments().tick(self.config)
        self.assertEqual(self.entry()['status'], 'running')

    def test_discussion_leases_hold_only_their_own_account_or_unknown_identity(self):
        with self.database.write() as db:
            db.execute("INSERT INTO discussions(id,title,created_at,updated_at) VALUES('discussion','Question',100,100)")
            db.execute("INSERT INTO discussion_executions(id,discussion_id,phase,created_at) VALUES('execution','discussion','running',100)")
            db.execute("INSERT INTO discussion_leases VALUES('discussion','execution','fence',100)")
        config = {**self.config, 'provider_auth_file_env': 'FORGE_HARNESS_AUTH_TWO'}
        self.assertIsNone(self.available_account(config))
        with self.database.write() as db:
            db.execute("INSERT INTO discussion_runtime(execution_id,manifest_json) VALUES('execution',?)",
                       (json.dumps({'runner_config': {'provider_auth_file_env': 'FORGE_HARNESS_AUTH_ONE'}}),))
        self.assertIsNotNone(self.available_account(config))
        self.assertIsNone(self.available_account({**config, 'provider_auth_file_env': 'FORGE_HARNESS_AUTH_ONE'}))

    def test_an_api_key_configuration_needs_no_lease(self):
        self.config['provider_auth_file_env'] = None
        self.pipeline_job('FORGE_HARNESS_AUTH_ONE')
        self.deployments().tick(self.config)
        self.assertEqual(self.entry()['status'], 'running')

    def test_a_running_strategist_holds_its_own_coding_account(self):
        # The strategist's own auxiliary lease carries the account, so a second
        # deployments store sees it as held until the execution is cleaned.
        self.config.update(provider='claude', provider_auth_file_env='FORGE_HARNESS_AUTH_ONE')
        self.deployments().tick(self.config)
        self.assertIsNone(self.available_account(self.config))
        self.finish()
        self.deployments().tick(self.config)
        self.assertIsNotNone(self.available_account(self.config))

    def memory_retry(self, state):
        with self.database.write() as db:
            unit_id = unit(db, 7, 5)
            db.execute("INSERT INTO unit_environment_observations(unit_id,memory_retry_json) VALUES(?,?)",
                       (unit_id, json.dumps({'tier': '8g', 'at': 900})))
            if state != 'building':
                db.execute("UPDATE unit_issues SET state='done',completion_issue_version=1,left_at=100,exit_reason='merged' WHERE unit_id=?", (unit_id,))
                db.execute("INSERT INTO candidates VALUES(?,?,10,'branch','head','base','policy','requirements',1,'inputs',100)",
                           ('candidate-' + unit_id, unit_id))
                db.execute('UPDATE units SET current_candidate_id=? WHERE id=?', ('candidate-' + unit_id, unit_id))
                db.execute("UPDATE units SET state='reviewing' WHERE id=?", (unit_id,))
                db.execute("UPDATE units SET state='merged',merge_sha='merge' WHERE id=?", (unit_id,))

    def test_a_pipeline_retry_waiting_for_memory_keeps_the_capacity(self):
        self.memory_retry('building')
        self.deployments().tick({**self.config, 'memory_tiers': ['4g', '8g']})
        self.assertFalse(self.engine)
        self.assertFalse(self.github.reads)
        # Without memory tiers there is no larger retry to drain for.
        self.deployments().tick(self.config)
        self.assertEqual(self.entry()['status'], 'running')

    def test_inadmissible_or_paused_unit_does_not_drain_strategist_capacity(self):
        self.memory_retry('building')
        config = {**self.config, 'memory_tiers': ['4g', '8g']}
        self.settings.rows[0]['dispatch_allowed'] = False
        self.assertFalse(self.deployments().memory_draining(config))
        self.settings.rows[0]['dispatch_allowed'] = True
        self.assertTrue(self.deployments().memory_draining(config))
        with self.database.write() as db:
            db.execute("INSERT INTO unit_gates(id,unit_id,subject_key,code,created_at) "
                       "VALUES('pause','unit-7-5','unit','operator_pause',100)")
        self.assertFalse(self.deployments().memory_draining(config))

    def test_a_finished_unit_never_holds_memory_capacity(self):
        self.memory_retry('merged')
        self.deployments().tick({**self.config, 'memory_tiers': ['4g', '8g']})
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
        identifier = self.entry()['job']['id']
        self.now = self.entry()['job']['deadline'] + 1
        self.deployments().tick(self.config)
        entry = self.entry()
        self.assertEqual(entry['status'], 'failed')
        self.assertIn('capacity', entry['summary'])
        # A cleaned execution is history, not a live job.
        self.assertIsNone(entry['job'])
        self.assertEqual(self.execution(identifier), {'domain': 'deployment', 'owner_id': '7', 'phase': 'cleaned',
                                                      'fenced': 1, 'cleanup': 1})
        self.assertEqual(self.resets, [identifier])
        self.assertFalse(self.engine)

    def test_reading_the_store_never_writes_it(self):
        self.deployments().tick(self.config)

        def dump():
            with self.database.read_snapshot() as db:
                return [tuple(row) for table in ('deployment_profiles', 'auxiliary_executions', 'auxiliary_runtime',
                                                 'auxiliary_leases', 'runtime_controls', 'repositories')
                        for row in db.execute('SELECT * FROM ' + table)]
        before = dump()
        for _ in range(3):
            self.deployments().read()
        self.assertEqual(dump(), before)

    def test_unknown_profile_fields_are_refused(self):
        with self.assertRaises(ValueError):
            self.deployments().save(7, 'example/site', domain='academy.example.org')
        self.assertFalse(self.deployments().read()['repositories'])


class ContractTests(unittest.TestCase):
    def test_strategist_note_is_separate_from_provider_credential_ipc(self):
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
            runner._prepare_credentials(job)
        with tarfile.open(fileobj=BytesIO(run.call_args.kwargs['input'])) as archive:
            self.assertEqual(archive.getnames(), ['credentials.json'])
            self.assertNotIn('GH_TOKEN', json.load(archive.extractfile('credentials.json')))
        from forge.factory.contracts import strategist_note
        note = strategist_note(job)
        self.assertEqual(note['role'], 'strategist')
        self.assertEqual(note['head'], 'c' * 40)
        self.assertIsNone(note['candidate'])
        self.assertIn('Read only', note['instructions'])
        self.assertNotIn('issue_number', note)

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


# The selected-store service runs the pipeline cycle through this function.
PIPELINE_CYCLE = 'forge.factory.sqlite_runtime.cycle'


class ServiceHookTests(unittest.TestCase):
    """The supervisor cycle drives deployments; it never fails because of them."""

    def setUp(self):
        from forge.app_access import ROLE_PERMISSIONS
        from forge.onboarding import Onboarding, ROLES
        from forge.settings import digest
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = selected_store(self.root)
        self.api = Mock()
        self.api.jwt.return_value = 'synthetic-app-jwt'
        self.setup = Onboarding(self.root, client=self.api)
        self.setup.read()
        self.api.discover.side_effect = lambda connection: {
            'repositories': [{'id': 10, 'name': 'example/project'}],
            'permissions': ROLE_PERMISSIONS[ROLES[connection['app_id'] - 1]]}
        settings = self.setup.settings()
        config = settings.load()
        config['connections'] = {role: {'app_id': index, 'installation_id': 100 + index,
                                        'private_key_env': 'FORGE_SETUP_' + role.upper() + '_KEY'}
                                 for index, role in enumerate(ROLES, 1)}
        settings.save(config, digest(settings.load()))
        settings.refresh()
        with self.setup.store.transaction() as data:
            data['apps'] = {role: {'connection': config['connections'][role]} for role in ROLES}
            data['owner'], data['account_type'] = 'example', 'organization'
        self.setup.select(10)
        self.setup.connect('codex', api_key='test-provider-value-no-real-credential')
        runtime = self.setup.runtime(); runtime['image'] = 'sha256:' + 'a' * 64
        self.setup.save_runtime(runtime, active=False)

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
            def prepare(self, job): pass
            def start(self, job):
                engine[job['id']].update(status='exited' if job.get('kind') == 'provider' else 'running',
                                         running=job.get('kind') != 'provider')
            def capture(self, job): return {'result': {'ready': True, 'attempt': job['id']}, 'logs': '', 'missing': []}
            def reset(self, job): engine.pop(job['id'], None)

        github = Mock()
        github.return_value.call.return_value = {'id': 10, 'default_branch': 'main'}
        github.return_value.protection_report.return_value = {'branch': 'main', 'available': True,
            'requirements': [{'id': 'review', 'label': 'Required review', 'passed': True}], 'bypass': {'status': 'ready'}}
        service = Service(self.root, runner=Runner, github=github)
        service.setup = self.setup
        return service

    def activated(self):
        service = self.service()
        self.setup.queue('check', self.setup.view()['revision'])
        for _ in range(30):
            if not self.setup.read()['operation']:
                break
            service.tick()
        self.setup.queue('start', self.setup.view()['revision'])
        service.tick()
        self.assertEqual(self.setup.read()['phase'], 'active')
        return service

    def cycle(self, service, **kwargs):
        """One service tick past the cycle gate, with the pipeline cycle stubbed."""
        from forge.settings import Settings
        with patch(PIPELINE_CYCLE, return_value={'at': 0}), patch.object(Settings, 'start_refresh'), \
                patch('forge.deployments.Deployments', **kwargs) as deployments:
            service.next_cycle = 0
            service.tick()
        return deployments

    def test_the_supervisor_cycle_ticks_deployments_with_the_runtime_configuration(self):
        service = self.activated()
        deployments = self.cycle(service)
        deployments.return_value.tick.assert_called_once()
        self.assertEqual(deployments.return_value.tick.call_args.args[0]['image'], 'sha256:' + 'a' * 64)
        self.assertEqual(Path(deployments.call_args.args[0]), self.root)
        self.assertLess(deployments.call_args.kwargs['github'].deadline, time.monotonic() + 30)
        self.assertGreater(deployments.call_args.kwargs['github'].deadline, time.monotonic())

    def test_a_deployment_failure_is_recorded_and_never_breaks_the_cycle(self):
        from forge.factory.runner import RunnerError
        service = self.activated()
        deployments = self.cycle(service, **{'return_value.tick.side_effect': RunnerError('Engine unavailable')})
        deployments.return_value.fail.assert_called_once_with('Deployment tick failed (RunnerError); retry pending')
        self.assertGreater(service.next_cycle, time.monotonic())
        deployments = self.cycle(service, **{'return_value.tick.side_effect': GitHubUnavailable('GitHub is unconfirmed')})
        deployments.return_value.fail.assert_called_once_with('Deployment tick failed (GitHubUnavailable); retry pending')
        deployments = self.cycle(service, **{'return_value.tick.side_effect': ValueError('bad profile')})
        deployments.return_value.fail.assert_called_once_with('Deployment tick failed (ValueError); retry pending')
        self.assertIsNone(self.setup.view()['error'])

    def test_unexpected_deployment_failure_still_ticks_discussions(self):
        service = self.activated()
        with patch('forge.factory.discussions.Discussions') as discussions:
            deployments = self.cycle(service, **{'return_value.tick.side_effect': TypeError('private details')})
        deployments.return_value.fail.assert_called_once_with('Deployment tick failed (TypeError); retry pending')
        discussions.return_value.tick.assert_called_once()

    def test_a_live_conversation_moves_between_cycles(self):
        service = self.activated()
        self.cycle(service)
        self.assertGreater(service.next_cycle, time.monotonic())
        # The service builds its discussions per tick; the selected store serves its own.
        with patch('forge.factory.discussions.Discussions') as discussions:
            discussions.return_value.live.return_value = False
            service.next_discussion = 0
            service.tick()
            discussions.return_value.tick.assert_not_called()
        with patch('forge.factory.discussions.Discussions') as discussions, patch(PIPELINE_CYCLE) as pipeline:
            discussions.return_value.live.return_value = True
            service.next_discussion = 0
            service.tick()
            service.tick()  # Driven every few seconds, not every service tick.
            tick = discussions.return_value.tick
            self.assertEqual(tick.call_count, 1)
            self.assertTrue(tick.call_args.kwargs.get('launch', True))
            pipeline.assert_not_called()  # Pipeline dispatch still waits for the cycle.

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
        Deployments(self.root, runner=Mock(), github=Mock(), settings=Mock()).fail('x' * 600)
        self.assertEqual(len(Deployments(self.root, runner=Mock(), github=Mock(), settings=Mock()).read()['error']), 500)


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
    """Only the reads observation is allowed to make."""

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
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = selected_store(self.root)
        with self.database.write() as db:
            db.execute("INSERT INTO repositories(id,full_name) VALUES(7,'example/site')")
        self.now = 1000.0
        self.github = ObservationGitHub()
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
        return self.deployments().observe(self.github, self.reader)

    def profile(self, **fields):
        self.settings.rows[0]['deployment']['profile'].update(fields)

    def merged(self, number=3, *, merge=MERGE, repo_id=7, name='example/site', state='merged'):
        """A unit whose current candidate GitHub merged; returns the candidate."""
        with self.database.write() as db:
            unit_id = unit(db, repo_id, number, name=name)
            candidate = 'candidate-' + unit_id
            db.execute("INSERT INTO candidates VALUES(?,?,2,'forge/work','" + 'a' * 40 + "','base','policy','requirements',1,'inputs',100)",
                       (candidate, unit_id))
            db.execute('UPDATE units SET current_candidate_id=? WHERE id=?', (candidate, unit_id))
            db.execute("INSERT INTO candidate_evidence(id,unit_id,candidate_id,kind,verdict,actor_id,observed_at,details_json) "
                       "VALUES(?,?,?,'merge','pass','github',950,?)",
                       ('merge-' + candidate, unit_id, candidate, json.dumps({'merge_sha': merge} if merge else {})))
            db.execute("UPDATE units SET state='reviewing' WHERE id=?", (unit_id,))
            if state == 'merged':
                db.execute("UPDATE unit_issues SET state='done',completion_issue_version=1,left_at=100,exit_reason='merged' WHERE unit_id=?", (unit_id,))
                db.execute("UPDATE units SET state='merged',merge_sha=? WHERE id=?", (merge or 'unrecorded', unit_id))
        return candidate

    def verdicts(self, candidate):
        with self.database.read_snapshot() as db:
            return [dict(status=row['status'], commit=row['commit_oid'], at=row['observed_at'], environment=row['environment'],
                         **json.loads(row['details_json']))
                    for row in db.execute('SELECT * FROM deployment_observations WHERE candidate_id=? ORDER BY observed_at,status',
                                          (candidate,))]

    def deployed(self, candidate):
        return next((row for row in self.verdicts(candidate) if row['status'] == 'success'), None)

    def entry(self, repo_id=7):
        return self.deployments().read()['repositories'][str(repo_id)]

    def deployment(self, sha, state, *, identifier=5, run_url='https://github.com/example/site/runs/1'):
        self.github.rows = [{'id': identifier, 'sha': sha, 'environment': 'production', 'created_at': '2026-09-24T10:00:00Z'}]
        self.github.statuses = {identifier: [{'state': state, 'created_at': '2026-09-24T10:05:00Z', 'log_url': run_url}]}

    def test_a_live_commit_equal_to_the_merge_marks_the_issue_deployed(self):
        candidate = self.merged()
        self.live = MERGE
        self.assertEqual(self.observe(), [])
        self.assertEqual(self.verdicts(candidate), [{'status': 'success', 'commit': MERGE, 'at': self.now,
                                                     'environment': 'production', 'via': 'live', 'run_url': None}])
        self.assertEqual(self.reads, [('https://site.example/health', 'health', 'commit', None)])
        self.assertEqual(self.entry()['observed']['live'], {'commit': MERGE, 'at': self.now})
        self.assertFalse(self.github.compared)
        # A verdict is recorded once; a later cycle neither rewrites nor re-announces it.
        self.now += 60
        self.observe()
        self.assertEqual(len(self.verdicts(candidate)), 1)
        self.assertEqual(self.deployed(candidate)['at'], 1000.0)

    def test_bad_candidate_does_not_prevent_later_deployment_observations(self):
        bad = self.merged(1, merge='d' * 40)
        good = self.merged(2)
        original = self.github.contains
        self.github.contains = Mock(side_effect=TypeError('private details'))
        self.live = MERGE
        with self.assertLogs('forge.deployments', level='WARNING') as logged:
            errors = self.observe()
        self.assertEqual(errors, [{'repo': 'example/site', 'reason': 'Deployment observation failed (TypeError); retry pending'}])
        self.assertIsNone(self.deployed(bad))
        self.assertIsNotNone(self.deployed(good))
        self.assertEqual(self.entry()['observed']['error'], errors[0]['reason'])
        self.assertIn(bad, logged.output[0])
        self.assertNotIn('private details', logged.output[0])
        self.github.contains = original
        self.live = 'd' * 40
        self.observe()
        self.assertIsNotNone(self.deployed(bad))
        self.assertIsNone(self.entry()['observed']['error'])

    def test_a_short_live_commit_still_matches_its_merge(self):
        candidate = self.merged()
        self.live = MERGE[:7]
        self.observe()
        self.assertEqual(self.deployed(candidate)['via'], 'live')
        self.assertFalse(self.github.compared)

    def test_a_live_commit_ahead_of_the_merge_marks_the_issue_deployed(self):
        candidate = self.merged()
        self.live = LIVE
        self.github.ancestors.add((MERGE, LIVE))
        self.observe()
        self.assertEqual(self.github.compared, [(7, MERGE, LIVE)])
        self.assertEqual(self.deployed(candidate)['commit'], LIVE)
        self.assertEqual(self.deployed(candidate)['via'], 'live')

    def test_a_live_commit_without_the_merge_leaves_the_issue_waiting(self):
        candidate = self.merged()
        self.live = LIVE
        self.assertEqual(self.observe(), [])
        self.assertEqual(self.verdicts(candidate), [])

    def test_a_successful_deployment_containing_the_merge_marks_it_deployed(self):
        candidate = self.merged()
        self.profile(exposure={'kind': 'none', 'path': None, 'field': None, 'header': None}, domain=None)
        self.deployment(LIVE, 'success')
        self.github.ancestors.add((MERGE, LIVE))
        self.assertEqual(self.observe(), [])
        self.assertFalse(self.reads)
        self.assertEqual(self.verdicts(candidate), [{'status': 'success', 'commit': LIVE, 'at': self.now,
                                                     'environment': 'production', 'via': 'github',
                                                     'run_url': 'https://github.com/example/site/runs/1'}])
        self.assertEqual(self.entry()['observed']['deploy'],
                         {'sha': LIVE, 'state': 'success', 'at': '2026-09-24T10:05:00Z',
                          'run_url': 'https://github.com/example/site/runs/1'})
        from forge.app_access import OPTIONAL_PERMISSIONS
        self.assertIn(('controller', 7, False, OPTIONAL_PERMISSIONS['controller']), self.github.tokens)

    def test_a_pending_deployment_is_not_a_verdict(self):
        candidate = self.merged()
        self.deployment(LIVE, 'in_progress')
        self.github.ancestors.add((MERGE, LIVE))
        self.observe()
        self.assertEqual(self.verdicts(candidate), [])
        self.assertEqual(self.entry()['observed']['deploy']['state'], 'in_progress')

    def test_a_failed_deployment_is_recorded_until_a_later_success(self):
        candidate = self.merged()
        self.deployment(LIVE, 'failure')
        self.github.ancestors.add((MERGE, LIVE))
        self.observe()
        self.assertEqual([(row['status'], row['commit'], row['run_url']) for row in self.verdicts(candidate)],
                         [('failure', LIVE, 'https://github.com/example/site/runs/1')])
        self.assertIsNone(self.deployed(candidate))
        # The same failure seen again is the same observation, not a second one.
        self.now += 30
        self.observe()
        self.assertEqual(len(self.verdicts(candidate)), 1)
        self.now += 60
        self.deployment(LIVE, 'success', identifier=6)
        self.observe()
        self.assertEqual([row['status'] for row in self.verdicts(candidate)], ['failure', 'success'])
        self.assertEqual(self.deployed(candidate)['via'], 'github')
        # Once deployed, the candidate is no longer observed.
        self.github.compared.clear()
        self.observe()
        self.assertFalse(self.github.compared)

    def test_a_deployment_that_predates_the_merge_says_nothing_about_it(self):
        candidate = self.merged()
        self.deployment('d' * 40, 'failure')
        self.observe()
        self.assertEqual(self.verdicts(candidate), [])
        self.assertEqual(self.github.compared, [(7, MERGE, 'd' * 40)])
        self.github.rows = []
        self.observe()
        self.assertIsNone(self.entry()['observed']['deploy'])

    def test_a_domain_failure_is_reported_and_retried_with_a_backoff(self):
        from forge.factory.live import LiveUnavailable
        candidate = self.merged()
        self.live_error = LiveUnavailable('The deployment did not answer in time')
        for _ in range(3):
            self.assertEqual(self.observe(), [{'repo': 'example/site',
                                               'reason': 'The deployment did not answer in time'}])
            self.now += 60
        self.assertEqual(len(self.reads), 3)
        self.assertEqual(self.verdicts(candidate), [])
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
        self.assertEqual(self.deployed(candidate)['via'], 'live')
        self.assertIsNone(self.entry()['observed']['error'])
        self.assertEqual(self.entry()['observed']['failures'], 0)

    def test_a_repository_without_a_confirmed_profile_is_never_observed(self):
        candidate = self.merged()
        self.settings.rows[0]['deployment'] = {'status': 'proposed', 'profile': None}
        self.assertEqual(self.observe(), [])
        self.assertFalse(self.reads)
        self.assertFalse(self.github.tokens)
        self.assertFalse(self.deployments().read()['repositories'])
        self.assertEqual(self.verdicts(candidate), [])

    def test_a_disabled_repository_is_never_observed(self):
        self.merged()
        self.settings.rows[0]['enabled'] = False
        self.assertEqual(self.observe(), [])
        self.assertFalse(self.reads)

    def test_a_refused_deployments_token_never_stops_the_domain_read(self):
        candidate = self.merged()
        self.settings.roles = {'controller': {'missing_recommended': ['deployments: read']}}
        self.github.token_error = GitHubUnavailable('Role credential could not be issued for this repository')
        self.live = MERGE
        errors = self.observe()
        self.assertEqual(errors, [{'repo': 'example/site',
                                   'reason': 'GitHub deployments need the recommended permission'}])
        self.assertTrue(self.reads)
        self.assertEqual(self.deployed(candidate)['via'], 'live')

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

    def test_an_unavailable_ancestry_is_reported_and_never_a_verdict(self):
        candidate = self.merged()
        self.live = LIVE
        self.github.contains = Mock(side_effect=GitHubUnavailable('GitHub comparison is unconfirmed'))
        self.assertEqual(self.observe(), [{'repo': 'example/site', 'reason': 'GitHub comparison is unconfirmed'}])
        self.assertEqual(self.verdicts(candidate), [])

    def test_a_merge_without_a_recorded_commit_yields_no_verdict(self):
        # The merge commit is read from the merge evidence, never from the pull request.
        candidate = self.merged(merge=None)
        self.live = MERGE
        self.observe()
        self.assertEqual(self.verdicts(candidate), [])
        self.assertFalse(self.github.compared)

    def test_only_merged_issues_are_ever_marked_deployed(self):
        candidate = self.merged(4, state='reviewing')
        self.live = MERGE
        self.observe()
        self.assertEqual(self.verdicts(candidate), [])

    def test_another_repository_merge_is_never_judged_by_this_domain(self):
        candidate = self.merged(9, repo_id=8, name='example/api')
        self.live = MERGE
        self.observe()
        self.assertEqual(self.verdicts(candidate), [])

    def test_every_merged_candidate_of_the_repository_gets_its_verdict(self):
        first, second = self.merged(3), self.merged(4, merge='e' * 40)
        self.live = LIVE
        self.github.ancestors.update({(MERGE, LIVE), ('e' * 40, LIVE)})
        self.observe()
        self.assertEqual(self.deployed(first)['commit'], LIVE)
        self.assertEqual(self.deployed(second)['commit'], LIVE)

    def test_the_observation_stops_at_the_cycle_budget(self):
        import time as clock
        self.merged()
        self.github.deadline = clock.monotonic() - 1
        self.assertEqual(self.observe(), [])
        self.assertFalse(self.reads)

    def test_the_observation_is_readable_per_repository(self):
        from forge.store.operations import deployment_views
        self.merged()
        self.live = MERGE
        self.observe()
        observed = deployment_views(self.database)['7']['observed']
        self.assertEqual({field: observed[field] for field in ('live', 'deploy', 'error')},
                         {'live': {'commit': MERGE, 'at': self.now}, 'deploy': None, 'error': None})
        self.assertEqual(deployment_views(self.database)['7']['name'], 'example/site')


if __name__ == '__main__':
    unittest.main()
