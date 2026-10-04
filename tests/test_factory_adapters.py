import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
import unittest
from unittest.mock import Mock, patch
import uuid

from forge.app_access import AppClient, AccessError, ROLE_PERMISSIONS, OPTIONAL_PERMISSIONS
from forge.factory.config import validate
from forge.factory.github import GitHub, GitHubUnavailable
from forge.factory.runner import ContainerRunner, RunnerError, process_isolation
from forge.settings import validate as validate_settings, digest
from tests.test_settings import configured


class AdapterTests(unittest.TestCase):
    def test_native_process_telemetry_requires_the_dedicated_policy_for_both_harnesses(self):
        for provider in ('codex', 'claude'):
            with self.assertRaises(RunnerError):
                process_isolation({'provider': provider, 'apparmor_profile': None})
            args = process_isolation({'provider': provider, 'apparmor_profile': 'forge-runner-private-proc'})
            self.assertIn('systempaths=unconfined', args)
            self.assertIn('apparmor=forge-runner-private-proc', args)

    def test_ci_snapshot_is_commit_bound_and_uses_controller_read_evidence(self):
        github, _, _ = self.github()
        job = {'repo': 'example/project', 'repo_id': 1, 'candidate': {'head': 'a'*40, 'base': 'b'*40}}
        def runs(role, item, path, key):
            sha = 'a'*40 if 'a'*40 in path else 'b'*40
            return [{'id': 10, 'name': 'ci', 'head_sha': sha, 'status': 'completed', 'conclusion': 'failure',
                     'app': {'id': 99}, 'output': {'summary': 'Tests failed', 'annotations_count': 1}}]
        github.pages = Mock(side_effect=runs)
        github.call = Mock(return_value=[{'path': 'test.ts', 'start_line': 73, 'message': 'Expected [Circular]'}])
        evidence = github.ci_evidence(job)
        self.assertEqual(evidence['head'], 'a'*40)
        self.assertEqual(evidence['checks']['a'*40][0]['annotations'][0]['message'], 'Expected [Circular]')
        self.assertTrue(all(call.args[0] == 'controller' for call in github.call.call_args_list))
        github.pages = Mock(return_value=[{'head_sha': 'c'*40}])
        with self.assertRaises(GitHubUnavailable):
            github.ci_evidence(job)

    def github(self):
        settings = Mock()
        settings.load.return_value = configured()
        client = Mock()
        client.jwt.return_value = 'synthetic-jwt'
        client.request.return_value = {'token': 'synthetic-installation-token'}
        return GitHub(settings, validate({}), client=client), client, settings

    def test_repair_launch_preserves_completed_check_until_a_new_verdict(self):
        github, _, _ = self.github()
        app_id = github.connection('controller')['app_id']
        completed = {'id': 123, 'name': 'forge/merge-policy', 'head_sha': 'a'*40,
                     'app': {'id': app_id}, 'external_id': 'candidate-key',
                     'status': 'completed', 'conclusion': 'failure'}
        github.pages = Mock(return_value=[completed])
        github.call = Mock()
        candidate = {'head': 'a'*40}
        self.assertEqual(github.check({}, 'controller', candidate, 'candidate-key', None,
                                      'Candidate is being processed'), 123)
        github.call.assert_not_called()
        # A final verdict still updates and verifies the same owned check.
        github.pages.side_effect = [[completed], [{**completed, 'conclusion': 'success'}]]
        self.assertEqual(github.check({}, 'controller', candidate, 'candidate-key', 'success',
                                      'Current policy passed'), 123)
        self.assertEqual(github.call.call_args.args[2], 'PATCH')

    def test_worker_tokens_are_fresh_repository_scoped_and_read_only_for_evaluators(self):
        github, client, _ = self.github()
        for role in ('review', 'audit', 'recovery'):
            github.token(role, 71, worker=True)
            body = client.request.call_args.args[-1]
            self.assertEqual(body['repository_ids'], [71])
            self.assertEqual(set(body['permissions'].values()), {'read'})
        github.token('build', 71, worker=True); github.token('build', 71, worker=True)
        self.assertEqual(client.request.call_count, 5)
        self.assertNotIn('administration', client.request.call_args.args[-1]['permissions'])

    def test_controller_asks_for_optional_permissions_only_when_a_caller_needs_them(self):
        github, client, _ = self.github(); github.token('controller', 71)
        permissions = client.request.call_args.args[-1]['permissions']
        self.assertEqual(permissions['administration'], 'read')
        # A live installation without the new grant must keep minting controller tokens.
        self.assertNotIn('deployments', permissions)
        self.assertEqual({k for k, v in permissions.items() if v != 'read'},
                         {'contents', 'issues', 'pull_requests', 'checks'})
        github.token('controller', 71, extra=OPTIONAL_PERMISSIONS['controller'])
        widened = client.request.call_args.args[-1]['permissions']
        self.assertEqual(widened['deployments'], 'read')
        self.assertEqual({k: v for k, v in widened.items() if k != 'deployments'}, permissions)
        # A widened token is never served from the narrow token's cache, or the reverse.
        self.assertEqual(client.request.call_count, 2)
        github.token('controller', 71); github.token('controller', 71, extra=OPTIONAL_PERMISSIONS['controller'])
        self.assertEqual(client.request.call_count, 2)

    def test_filing_a_deployment_issue_verifies_the_number_it_reports(self):
        github, client, _ = self.github()
        item = {'repo': 'example/project', 'repo_id': 71}
        client.request.side_effect = [{'token': 'synthetic-installation-token'}, {'number': 41}]
        self.assertEqual(github.file_issue(item, 'Expose the build commit', 'body', ['forge:deployment']), 41)
        method, path, _, payload = client.request.call_args.args
        self.assertEqual((method, path), ('POST', '/repos/example/project/issues'))
        self.assertEqual(payload, {'title': 'Expose the build commit', 'body': 'body', 'labels': ['forge:deployment']})
        for response in ({}, {'number': '41'}, None):
            client.request.side_effect = [{'token': 'synthetic-installation-token'}, response]
            github.tokens.clear()
            with self.assertRaises(GitHubUnavailable):
                github.file_issue(item, 'Expose the build commit', 'body', ['forge:deployment'])

    def test_multiple_installations_select_by_repository_id(self):
        github, client, settings = self.github()
        config = configured(); connection = config['connections']['build']
        connection.pop('installation_id'); connection['installation_ids'] = [101, 202]
        validate_settings(config); settings.load.return_value = config
        settings.observation.return_value = {'roles': {'build': {'connection': digest(connection),
                                                                  'repositories': [{'id': 71, 'installation_id': 202}]}}}
        github.token('build', 71, worker=True)
        self.assertEqual(client.request.call_args.args[1], '/app/installations/202/access_tokens')
        with self.assertRaises(GitHubUnavailable): github.token('build', 99, worker=True)

    def test_app_discovery_merges_installations_without_enabling_repos(self):
        client = AppClient()
        client._discover_one = Mock(side_effect=[
            {'repositories': [{'id': 1, 'name': 'example/project'}], 'app_slug': 'builder', 'account': 'example', 'permissions': {'contents': 'write'}, 'installation_url': 'one'},
            {'repositories': [{'id': 2, 'name': 'person/project'}], 'app_slug': 'builder', 'account': 'person', 'permissions': {'contents': 'read'}, 'installation_url': 'two'}])
        data = client.discover({'app_id': 1, 'installation_ids': [10, 20], 'private_key_env': 'SYNTHETIC_KEY_REF'})
        self.assertEqual([r['installation_id'] for r in data['repositories']], [10, 20])
        self.assertEqual(data['permissions']['contents'], 'read')
        self.assertTrue(all('enabled' not in r for r in data['repositories']))

    def test_stale_bot_author_and_dismissed_approvals_do_not_satisfy_human_gate(self):
        github, _, _ = self.github(); github.runtime['human_reviewers'] = ['human', 'author', 'robot']
        pr = {'number': 2, 'head': {'sha': 'a'*40}, 'user': {'login': 'author'}}
        def review(identifier, login, state, sha='a'*40, kind='User'):
            return {'id': identifier, 'user': {'login': login, 'type': kind}, 'state': state, 'commit_id': sha}
        for reviews in ([review(1,'human','APPROVED','b'*40)], [review(1,'author','APPROVED')],
                        [review(1,'robot','APPROVED',kind='Bot')],
                        [review(1,'human','APPROVED'),review(2,'human','DISMISSED')]):
            github.pages = Mock(return_value=reviews)
            self.assertFalse(github.human({},pr)[0])
        github.pages.return_value = [review(1,'human','APPROVED'),review(2,'colleague','CHANGES_REQUESTED')]
        self.assertEqual(github.human({},pr),(False,True))

    def test_without_an_allowlist_any_person_with_write_access_approves(self):
        github, _, _ = self.github(); github.runtime['human_reviewers'] = []
        pr = {'number': 2, 'head': {'sha': 'a'*40}, 'user': {'login': 'author'}}
        github.pages = Mock(return_value=[{'id': 1, 'user': {'login': 'Maintainer', 'type': 'User'}, 'state': 'APPROVED',
                                           'commit_id': 'a'*40, 'submitted_at': '2026-10-01T16:03:21Z'}])
        for permission, counted in (('write', True), ('admin', True), ('read', False), ('none', False)):
            github.call = Mock(return_value={'permission': permission})
            row = github.approvals({}, pr)[0]
            self.assertEqual((row['counted'], github.human({}, pr)[0]), (counted, counted))
            self.assertIn('/collaborators/Maintainer/permission', github.call.call_args.args[3])
        self.assertEqual(row['why'], 'No write access to the repository')
        github.call = Mock(side_effect=GitHubUnavailable('Hidden', status=404))
        self.assertFalse(github.human({}, pr)[0])
        github.runtime['human_reviewers'] = ['someone-else']
        github.call = Mock()
        self.assertEqual(github.approvals({}, pr)[0]['why'], 'Not on the approver list in Settings')
        github.call.assert_not_called()
        github.runtime['human_reviewers'] = ['maintainer']
        self.assertTrue(github.human({}, pr)[0])

    def test_settling_checks_cancels_only_forges_own_unfinished_runs(self):
        github, _, _ = self.github()
        github.connection = lambda role: {'app_id': {'review': 2, 'audit': 3, 'controller': 4}[role]}
        github.pages = Mock(return_value=[
            {'id': 1, 'name': 'forge/audit', 'status': 'in_progress', 'app': {'id': 3}},
            {'id': 2, 'name': 'forge/merge-policy', 'status': 'in_progress', 'app': {'id': 4}},
            {'id': 3, 'name': 'forge/review', 'status': 'completed', 'app': {'id': 2}},
            {'id': 4, 'name': 'ci', 'status': 'in_progress', 'app': {'id': 99}},
            {'id': 5, 'name': 'forge/audit', 'status': 'in_progress', 'app': {'id': 77}}])
        github.call = Mock()
        self.assertEqual(github.settle_checks({}, {'head': 'a' * 40}, 'Paused'), 2)
        calls = [(c.args[0], c.args[2], c.args[3], c.args[4]['conclusion']) for c in github.call.call_args_list]
        self.assertEqual(calls, [('audit', 'PATCH', '/check-runs/1', 'cancelled'), ('controller', 'PATCH', '/check-runs/2', 'cancelled')])

    def test_shared_repository_allows_human_access_and_owner_chosen_checks(self):
        github, _, _ = self.github()
        protection = {'required_pull_request_reviews': {'dismiss_stale_reviews':True,
            'required_approving_review_count':2, 'bypass_pull_request_allowances':{'users':[{'login':'maintainer'}], 'apps':[{'id':4}]}},
            'enforce_admins':{'enabled':False}, 'restrictions': {'apps':[{'id':4},{'id':9}], 'users':[{'login':'developer'}], 'teams':[{'id':12}]},
            'required_status_checks':{'strict':False,'checks':[{'context':'project-ci','app_id':99}]}}
        github.call = Mock(return_value=protection)
        github.pages = Mock(return_value=[])
        self.assertTrue(github.protections({}, {'base_ref':'main'}))
        protection['required_pull_request_reviews']['dismiss_stale_reviews'] = False
        self.assertFalse(github.protections({}, {'base_ref':'main'}))
        protection['required_pull_request_reviews']['dismiss_stale_reviews'] = True
        protection['required_pull_request_reviews']['required_approving_review_count'] = 1
        self.assertFalse(github.protections({}, {'base_ref':'main'}))

    def test_forge_still_requires_its_own_current_review_and_audit_evidence(self):
        github, _, _ = self.github()
        item={'candidate':{'head':'a'*40},'evidence':{'review':{'check_id':21},'audit':{'check_id':22}}}
        runs=[{'name':'forge/review','id':21,'app':{'id':2},'status':'completed','conclusion':'success'},
              {'name':'forge/audit','id':22,'app':{'id':3},'status':'completed','conclusion':'success'}]
        github.pages=Mock(return_value=runs)
        self.assertTrue(github.gates(item))
        runs[0]['app']['id']=999
        self.assertFalse(github.gates(item))
        runs[0]['app']['id']=2
        runs[1]['conclusion']='failure'
        self.assertFalse(github.gates(item))

    def test_hung_graceful_stop_still_forces_exact_container_removal(self):
        runner = ContainerRunner(validate({}), run=Mock())
        job = {'id':str(uuid.uuid4()),'slot':'build-1','fenced':True}
        runner.inspect = Mock(side_effect=[{'id':'exact-container','running':True},None])
        runner.ready = Mock(return_value=True)
        def call(args, **kwargs):
            if args[0]=='stop': raise RunnerError('Timeout')
            return Mock(returncode=0)
        runner.call = Mock(side_effect=call)
        runner.reset(job)
        self.assertIn(unittest.mock.call(['rm','--force','--volumes','exact-container'], timeout=30), runner.call.call_args_list)

    def test_engine_error_is_not_container_absence(self):
        runner = ContainerRunner(validate({}), run=Mock(return_value=Mock(returncode=1,stdout=b'')))
        with self.assertRaises(RunnerError): runner.inspect({'id':str(uuid.uuid4()),'slot':'build-1'})

    def test_elapsed_time_uses_container_start_not_assignment_time(self):
        runner = ContainerRunner(validate({}), run=Mock())
        job = {'id':str(uuid.uuid4()), 'slot':'build-1', 'created_at':1}
        name = runner.name(job)
        value = {'Name':'/'+name, 'Id':'one',
                 'Config':{'Labels':{'forge.execution':job['id'], 'forge.slot':job['slot']}},
                 'State':{'Status':'running', 'ExitCode':0, 'Running':True,
                          'StartedAt':'2026-09-18T00:00:00.123456789Z'}}
        def inspect(start):
            value['State']['StartedAt'] = start
            runner.call = Mock(side_effect=[Mock(stdout=json.dumps({'Names':name,'ID':'one'}).encode()),
                                           Mock(stdout=json.dumps([value]).encode())])
            return runner.inspect(job)
        self.assertAlmostEqual(inspect(value['State']['StartedAt'])['started_at'], 1789689600.123456, places=5)
        self.assertIsNone(inspect('0001-01-01T00:00:00Z')['started_at'])
        self.assertIsNone(inspect('unavailable')['started_at'])

    def test_forged_ownership_is_not_reset(self):
        job = {'id':str(uuid.uuid4()),'slot':'build-1','fenced':True}
        runner = ContainerRunner(validate({}), run=Mock())
        name=runner.name(job)
        runner.call=Mock(side_effect=[Mock(stdout=json.dumps({'Names':name,'ID':'one'}).encode()),
            Mock(stdout=json.dumps([{'Name':'/'+name,'Id':'one','Config':{'Labels':{'forge.execution':'different','forge.slot':'build-1'}}}]).encode())])
        with self.assertRaises(RunnerError): runner.reset(job)
        self.assertEqual(runner.call.call_count,2)

    def test_public_network_is_rejected_before_admission(self):
        runner = ContainerRunner(validate({'image':'sha256:'+'a'*64, 'seccomp_profile_env': None}), run=Mock())
        runner.call = Mock(side_effect=[Mock(stdout=b'26'), Mock(stdout=b'[{"Config":{"Labels":{"org.forge.build-environment":"1"}}}]'),
                                        Mock(stdout=b'[{"Internal": false}]')])
        with self.assertRaisesRegex(RunnerError, 'network must be internal'): runner.ready()

    def test_image_without_build_contract_cannot_dispatch(self):
        runner = ContainerRunner(validate({'image':'sha256:'+'a'*64, 'seccomp_profile_env': None}), run=Mock())
        runner.call = Mock(return_value=Mock(stdout=b'[{"Config":{"Labels":{}}}]'))
        with self.assertRaisesRegex(RunnerError, 'build-environment contract'): runner.ready()

    def test_old_human_review_on_same_head_does_not_cover_new_policy(self):
        github, _, _ = self.github(); github.runtime['human_reviewers'] = ['human']
        github.pages = Mock(return_value=[{'id':1, 'state':'APPROVED', 'commit_id':'a'*40,
            'user':{'login':'human','type':'User'}, 'submitted_at':'2026-09-11T10:00:00Z'}])
        pr = {'number':2,'head':{'sha':'a'*40},'user':{'login':'builder[bot]'}}
        item = {'candidate': {'accepted_at': 1789120801}}
        self.assertFalse(github.human(item,pr)[0])
        github.pages.return_value[0]['submitted_at'] = '2026-09-11T11:00:00Z'
        self.assertTrue(github.human(item,pr)[0])

    def test_evidence_capture_has_a_hard_streaming_bound(self):
        import sys
        runner = ContainerRunner(validate({}))
        with self.assertRaises(RunnerError):
            runner._bounded([sys.executable,'-c','import sys;sys.stdout.write("x"*1048576)'],
                            {}, None, 5, 1024)

    def test_egress_rejects_private_dns_results_and_unlisted_domains(self):
        spec=importlib.util.spec_from_file_location('fixture_egress',Path(__file__).resolve().parents[1]/'deploy/runner/egress.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        resolver=Mock(return_value=[(2,1,6,'',('127.0.0.1',443))])
        with self.assertRaises(ValueError): module.destinations('api.github.com',resolver)
        with self.assertRaises(ValueError): module.destinations('attacker.example',resolver)
        resolver.return_value=[(2,1,6,'',('93.184.215.14',443))]
        self.assertTrue(module.destinations('api.github.com',resolver))
        self.assertFalse(module.allowed('github.com.attacker.example'))
        self.assertTrue(module.allowed('auth.openai.com'))
        self.assertFalse(module.allowed('auth.openai.com.attacker.example'))


if __name__ == '__main__': unittest.main()


class MergeRefusalTests(unittest.TestCase):
    def github(self):
        settings = Mock()
        settings.load.return_value = configured()
        client = Mock()
        client.jwt.return_value = 'synthetic-jwt'
        return GitHub(settings, validate({}), client=client), client

    def item(self):
        return {'repo': 'XRPL-Commons/hackathon', 'repo_id': 71, 'candidate': {'pr': 825, 'head': 'a' * 40}}

    def test_refused_merge_names_the_pull_request_and_the_rule(self):
        github, client = self.github()
        github.token = Mock(return_value='synthetic-token')
        client.request.side_effect = AccessError('GitHub returned HTTP 405; check installation access', status=405)
        with self.assertRaisesRegex(GitHubUnavailable, r'refused to merge XRPL-Commons/hackathon#825: branch rules still block it') as caught:
            github.merge(self.item())
        self.assertEqual(caught.exception.status, 405)

    def test_budget_exhaustion_keeps_the_generic_message(self):
        github, client = self.github()
        github.deadline = 0
        with self.assertRaisesRegex(GitHubUnavailable, 'budget reached'):
            github.merge(self.item())


class WorkerContractTests(unittest.TestCase):
    """Every role reports in plain language, and the image schema says so."""

    def note(self, role):
        return {'id': str(uuid.uuid4()), 'role': role, 'issue_number': 1, 'repo': 'example/project',
                'repo_id': 1, 'branch': 'forge/issue-1', 'publisher': role + '[bot]', 'target_branch': 'main',
                'candidate': {'key': 'candidate-key'} if role in ('review', 'audit') else None,
                'deadline': 10, 'history': {}, 'failure': {'attempt': 'a'} if role == 'recovery' else None}

    def test_every_role_must_report_a_headline_and_a_plain_description(self):
        from forge.factory.contracts import HEADLINE_LIMIT, PLAIN_LIMIT, ROLE_FIELDS
        from forge.factory.harness import schema
        for role in ('build', 'review', 'audit', 'recovery'):
            shape = schema(self.note(role))
            self.assertIn('headline', shape['required'])
            self.assertIn('plain', shape['required'])
            # The image duplicates the limits contracts.result() truncates at.
            self.assertEqual(shape['properties']['headline']['maxLength'], HEADLINE_LIMIT)
            self.assertEqual(shape['properties']['plain']['maxLength'], PLAIN_LIMIT)
            self.assertFalse(shape['additionalProperties'])
            self.assertEqual(set(shape['properties']) - {'version', 'attempt', 'role', 'outcome', 'blocker',
                                                         'summary', 'headline', 'plain', 'decision', 'required_services'}, set(ROLE_FIELDS[role]))

    def test_assignment_instructions_teach_the_tone_with_examples(self):
        from forge.factory.contracts import assignment_note
        instructions = assignment_note({**self.note('build'), 'review_policy': {}, 'ci_evidence': None})['instructions']
        self.assertIn('someone who does not read code', instructions)
        self.assertIn('No file paths, commands, variable names or code in either.', instructions)
        self.assertIn('Built and waiting for review', instructions)
        self.assertGreaterEqual(instructions.count('headline "'), 3)
