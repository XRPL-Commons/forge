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

from forge.app_access import AppClient, AccessError, ROLE_PERMISSIONS
from forge.factory.config import validate
from forge.factory.github import GitHub, GitHubUnavailable
from forge.factory.runner import ContainerRunner, RunnerError
from forge.settings import validate as validate_settings, digest
from tests.test_settings import configured


class AdapterTests(unittest.TestCase):
    def github(self):
        settings = Mock()
        settings.load.return_value = configured()
        client = Mock()
        client.jwt.return_value = 'synthetic-jwt'
        client.request.return_value = {'token': 'synthetic-installation-token'}
        return GitHub(settings, validate({}), client=client), client, settings

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

    def test_controller_requires_read_only_protection_inspection(self):
        github, client, _ = self.github(); github.token('controller', 71)
        self.assertEqual(client.request.call_args.args[-1]['permissions']['administration'], 'read')

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

    def test_shared_repository_allows_human_access_and_owner_chosen_checks(self):
        github, _, _ = self.github()
        protection = {'required_pull_request_reviews': {'dismiss_stale_reviews':True,
            'required_approving_review_count':2, 'bypass_pull_request_allowances':{'users':[{'login':'maintainer'}]}},
            'enforce_admins':{'enabled':False}, 'restrictions': {'apps':[{'id':4},{'id':9}], 'users':[{'login':'developer'}], 'teams':[{'id':12}]},
            'required_status_checks':{'strict':False,'checks':[{'context':'project-ci','app_id':99}]}}
        github.call = Mock(return_value=protection)
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

    def test_forged_ownership_is_not_reset(self):
        job = {'id':str(uuid.uuid4()),'slot':'build-1','fenced':True}
        runner = ContainerRunner(validate({}), run=Mock())
        name=runner.name(job)
        runner.call=Mock(side_effect=[Mock(stdout=json.dumps({'Names':name,'ID':'one'}).encode()),
            Mock(stdout=json.dumps([{'Name':'/'+name,'Id':'one','Config':{'Labels':{'forge.execution':'different','forge.slot':'build-1'}}}]).encode())])
        with self.assertRaises(RunnerError): runner.reset(job)
        self.assertEqual(runner.call.call_count,2)

    def test_public_network_is_rejected_before_admission(self):
        runner = ContainerRunner(validate({'image':'sha256:'+'a'*64}), run=Mock())
        runner.call = Mock(return_value=Mock(stdout=b'[{"Internal": false}]'))
        with self.assertRaises(RunnerError): runner.ready()

    def test_old_human_review_on_same_head_does_not_cover_new_policy(self):
        github, _, _ = self.github(); github.runtime['human_reviewers'] = ['human']
        github.pages = Mock(return_value=[{'id':1, 'state':'APPROVED', 'commit_id':'a'*40,
            'user':{'login':'human','type':'User'}, 'submitted_at':'2026-09-11T10:00:00Z'}])
        pr = {'number':2,'head':{'sha':'a'*40},'user':{'login':'builder[bot]'}}
        item = {'candidate': {'accepted_at': 1789120801}}
        self.assertFalse(github.human(item,pr)[0])
        github.pages.return_value[0]['submitted_at'] = '2026-09-11T11:00:00Z'
        self.assertTrue(github.human(item,pr)[0])

    def test_native_human_gate_rejects_unknown_codeowner_patterns(self):
        import base64
        github, _, _ = self.github(); github.runtime['human_reviewers'] = ['human']
        protection = {'required_pull_request_reviews':{'require_code_owner_reviews':True}}
        def evaluate(body):
            github.call = Mock(side_effect=[protection, {'content':base64.b64encode(body.encode()).decode()}])
            return github.native_human_gate({'candidate':{'base_ref':'main','base':'a'*40}})
        self.assertTrue(evaluate('* @human'))
        self.assertFalse(evaluate('* @someone-else'))
        self.assertFalse(evaluate('* @human\n/private/ @someone-else'))
        self.assertFalse(evaluate('* @organization/team'))

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
