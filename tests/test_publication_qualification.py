from copy import deepcopy
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from forge.app_access import AccessError
from forge.factory.publisher_runtime import SigningCapabilities
from forge.publication_qualification import Qualification, QualificationError, signing_context
from forge.store.configuration import Configuration
from forge.store.db import Store
from forge_client.cli import parser
from forge_client.publication_qualification import execute, HOST
from forge_client.contracts import SetupError


class Access:
    def __init__(self):
        self.permissions = {'contents': 'write', 'administration': 'read'}
        self.bot = {'id': 9, 'login': 'controller[bot]', 'type': 'Bot'}
        self.client = SimpleNamespace(request=self.request)
        self.scratch_token = 'private-token'
        self.writes = []
        self.rules = []
        self.current = 'a' * 40
        self.commits = {self.current: dict(sha=self.current, tree={'sha': 'b' * 40}, parents=[], verification={'verified': True}, message='base')}
        self.signed = True
        self.suite_result = 'pass'
        self.uncertain = False
        self.conn = {'app_id': 4, 'installation_id': 5}
        self.rule = dict(id=7, source_type='Organization', source='owner', enforcement='active', current_user_can_bypass='never',
            conditions={'ref_name': {'include': ['~ALL'], 'exclude': []}}, rules=[{'type': 'required_signatures'}], bypass_actors=[])

    def open(self, target, scratch, *, write=False):
        self.write_access = write
        return ({'id': 1, 'default_branch': 'main'}, {'id': 2, 'default_branch': 'main', 'node_id': 'R_probe'}, self.conn)

    def rule_reads(self, suffix):
        if suffix == '/branches/main': return {'name': 'main'}
        if suffix == '/branches/main/protection': raise AccessError('absent', status=404)
        if suffix.startswith('/rules/branches/'): return deepcopy(self.rules)
        if suffix == '/rulesets/7': return deepcopy(self.rule)
        raise AssertionError(suffix)

    def target(self, method, suffix):
        assert method == 'GET'
        return self.rule_reads(suffix)

    def scratch(self, method, suffix, payload=None):
        if method != 'GET':
            assert self.write_access
            self.writes.append((method, suffix, payload))
        if suffix.startswith('/git/ref/'): return {'ref': 'refs/heads/main', 'object': {'type': 'commit', 'sha': self.current}}
        if method == 'GET' and suffix.startswith('/git/commits/'): return deepcopy(self.commits[suffix.rsplit('/', 1)[-1]])
        if suffix.startswith('/commits/'): return {'sha': suffix.rsplit('/', 1)[-1], 'author': self.bot}
        if suffix == '/git/trees': return {'sha': 'c' * 40}
        if suffix == '/git/commits':
            result = self.create(payload['message'], payload['tree'], payload['parents'][0], self.signed)
            if self.uncertain: raise AccessError('unknown')
            return {'sha': result}
        if suffix.startswith('/rulesets/rule-suites?'):
            raw = self.commits[self.current]
            return [dict(id=8, before_sha=raw['parents'][0]['sha'], after_sha=self.current, ref='refs/heads/main', actor_id=9)]
        if suffix == '/rulesets/rule-suites/8':
            raw = self.commits[self.current]
            return dict(id=8, repository_id=2, before_sha=raw['parents'][0]['sha'], after_sha=self.current,
                ref='refs/heads/main', actor_id=9, result=self.suite_result,
                rule_evaluations=[dict(rule_type='required_signatures', rule_source={'id':7}, enforcement='active', result='pass')])
        return self.rule_reads(suffix)

    def create(self, message, tree, parent, signed):
        value = ('d' if len(self.commits) == 1 else 'e') * 40
        self.commits[value] = dict(sha=value, message=message, tree={'sha':tree}, parents=[{'sha':parent}], verification={'verified':signed})
        return value

    def request(self, method, path, token, payload):
        assert self.write_access and path == '/graphql'
        self.writes.append((method, path, payload))
        update = payload['variables']['input']['refUpdates'][0]
        assert update['beforeOid'] == self.current
        self.current = update['afterOid']
        return {'data': {'updateRefs': {'clientMutationId': None}}}

    def graphql(self, variables):
        assert self.write_access
        self.writes.append(('POST', '/graphql', variables))
        value = variables['input']
        self.current = self.create(value['message']['headline'], 'c' * 40, value['expectedHeadOid'], True)
        return self.current


class QualificationTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.store = Store.create(temp.name)
        self.access = Access()
        self.service = Qualification(self.store, self.access, clock=lambda: 123)

    def run_probe(self, **kw):
        return self.service.execute('owner/target', 'owner/scratch', **kw)

    def test_plan_has_no_repository_or_configuration_writes(self):
        result = self.run_probe()
        self.assertEqual(result['status'], 'planned')
        self.assertFalse(self.access.writes)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM configuration').fetchone()[0], 0)

    def test_signed_rest_no_custom_author_and_no_rule_claim_for_personal_target(self):
        result = self.run_probe(apply=True)
        self.assertEqual(result['status'], 'qualified', result)
        self.assertEqual(result['strategy'], 'rest')
        self.assertEqual(result['signing_requirement'], 'not_required')
        commit = next(payload for _, suffix, payload in self.access.writes if suffix == '/git/commits')
        self.assertEqual(set(commit), {'message', 'tree', 'parents'})
        proof = Configuration(self.store).get('publication_signing', '1')['settings']
        self.assertFalse(proof['signing_rule_accepted'])
        before = deepcopy(self.access.writes)
        again = self.run_probe(apply=True, receipt_id=result['receipt_id'])
        self.assertEqual(again['qualification_revision'], result['qualification_revision'])
        self.assertEqual(self.access.writes, before)
        reader = SigningCapabilities(self.store, SimpleNamespace(connection=lambda *a: self.access.conn,
            call=lambda role, job, method, suffix: self.access.target(method, suffix)))
        self.assertTrue(reader({'repo_id': 1, 'target_branch': 'main'})['qualified'])
        self.assertFalse(reader({'repo_id': 1, 'target_branch': 'other'})['qualified'])
        self.access.rules = [{'type':'required_signatures','ruleset_id':7}]
        self.assertFalse(reader({'repo_id': 1, 'target_branch': 'main'})['qualified'])

    def test_unsigned_rest_falls_back_only_after_exact_unsigned_readback(self):
        self.access.signed = False
        result = self.run_probe(apply=True)
        self.assertEqual(result['status'], 'qualified', result)
        self.assertEqual(result['strategy'], 'graphql')
        self.assertEqual(sum(s == '/git/commits' for _, s, _ in self.access.writes), 1)

    def test_uncertain_commit_never_repeats_or_falls_back(self):
        self.access.uncertain = True
        result = self.run_probe(apply=True)
        self.assertEqual(result['status'], 'pending')
        before = deepcopy(self.access.writes)
        again = self.run_probe(apply=True, receipt_id=result['receipt_id'])
        self.assertEqual(again['reason'], 'probe_evidence_incomplete')
        self.assertEqual(self.access.writes, before)
        self.assertIsNone(Configuration(self.store).get('publication_signing', '1'))

    def test_active_matching_rule_requires_exact_non_bypass_suite(self):
        self.access.rules = [{'type':'required_signatures','ruleset_id':7}]
        self.access.suite_result = 'bypass'
        result = self.run_probe(apply=True)
        self.assertEqual(result['reason'], 'rule_suite_not_accepted')
        writes = deepcopy(self.access.writes)
        self.access.suite_result = 'pass'
        result = self.run_probe(apply=True, receipt_id=result['receipt_id'])
        self.assertEqual(result['status'], 'qualified', result)
        self.assertEqual(result['signing_requirement'], 'accepted')
        self.assertEqual(self.access.writes, writes)

    def test_bypass_unknown_rule_and_inaccessible_classic_refuse_before_writes(self):
        self.access.rules = [{'type':'required_signatures','ruleset_id':7}]
        self.access.rule['current_user_can_bypass'] = 'always'
        with self.assertRaisesRegex(QualificationError, 'bypass'):
            self.run_probe(apply=True)
        self.assertFalse(self.access.writes)
        def denied(method, suffix):
            if suffix.endswith('/protection'): raise AccessError('hidden', status=403)
            return {'name': 'main'}
        with self.assertRaises(AccessError): signing_context(denied, 'main')

    def test_changed_commit_tree_ref_and_installation_cannot_import(self):
        result = self.run_probe(apply=True)
        self.access.commits[self.access.current]['tree']['sha'] = 'f' * 40
        self.assertEqual(self.run_probe(apply=True, receipt_id=result['receipt_id'])['reason'], 'probe_evidence_incomplete')
        self.access.conn['installation_id'] = 8
        with self.assertRaisesRegex(QualificationError, 'identity_changed'):
            self.run_probe(apply=True, receipt_id=result['receipt_id'])

    def test_lost_ref_response_reconciles_by_reads_without_replaying(self):
        request = self.access.client.request
        def lost(*args):
            request(*args)
            raise AccessError('lost response')
        self.access.client.request = lost
        result = self.run_probe(apply=True)
        self.assertEqual(result['stage'], 'ref_started')
        writes = deepcopy(self.access.writes)
        self.access.client.request = request
        result = self.run_probe(apply=True, receipt_id=result['receipt_id'])
        self.assertEqual(result['status'], 'qualified', result)
        self.assertEqual(self.access.writes, writes)

    def test_lost_graphql_commit_response_recovers_only_exact_host_marker(self):
        self.access.signed = False
        original = self.access.graphql
        def lost(*args):
            original(*args)
            raise AccessError('lost response')
        self.access.graphql = lost
        result = self.run_probe(apply=True)
        self.assertEqual(result['stage'], 'commit_started')
        writes = deepcopy(self.access.writes)
        self.access.graphql = original
        result = self.run_probe(apply=True, receipt_id=result['receipt_id'])
        self.assertEqual(result['status'], 'qualified', result)
        self.assertEqual(self.access.writes, writes)

    def test_classic_signature_404_is_absent_but_enabled_is_unsupported(self):
        def reads(method, suffix):
            if suffix == '/branches/main': return {'name':'main'}
            if suffix.endswith('/protection'): return {'enforce_admins': {'enabled': True}}
            if suffix.endswith('/required_signatures'): raise AccessError('absent', status=404)
            if '/rules/branches/' in suffix: return []
            self.fail(suffix)
        self.assertEqual(signing_context(reads, 'main'), [])
        def enabled(method, suffix):
            if suffix.endswith('/required_signatures'): return {'enabled':True}
            return reads(method, suffix)
        with self.assertRaisesRegex(QualificationError, 'classic_signing_rule_unsupported'):
            signing_context(enabled, 'main')

    def test_workflow_probe_uses_current_grants_and_does_not_change_ordinary_proof(self):
        qualified = self.run_probe(apply=True)
        config = Configuration(self.store)
        ordinary = config.get('publication_signing', '1')
        original = self.access.scratch
        def refused(method, suffix, payload=None):
            if method == 'POST' and suffix == '/git/trees' and payload['tree'][0]['path'].startswith('.github/workflows/'):
                self.access.writes.append((method, suffix, payload))
                raise AccessError('refused', status=403)
            return original(method, suffix, payload)
        self.access.scratch = refused
        result = self.run_probe(apply=True, receipt_id=qualified['receipt_id'], check_workflow=True)
        self.assertEqual(result['status'], 'observed')
        self.assertEqual(result['workflow_checks']['without_workflows']['status'], 'refused')
        self.assertEqual(result['positive_test'], 'existing_workflows_grant_missing')
        self.assertFalse(result['permission_cause_proven'])
        self.assertEqual(config.get('publication_signing', '1'), ordinary)
        writes = deepcopy(self.access.writes)
        self.run_probe(apply=True, receipt_id=qualified['receipt_id'], check_workflow=True)
        self.assertEqual(self.access.writes, writes)

    def test_lost_workflow_ref_reply_rechecks_without_another_write(self):
        qualified = self.run_probe(apply=True)
        original = self.access.client.request
        def lost(*args):
            original(*args)
            raise AccessError('lost reply')
        self.access.client.request = lost
        result = self.run_probe(apply=True, receipt_id=qualified['receipt_id'], check_workflow=True)
        self.assertEqual(result['workflow_checks']['without_workflows']['status'], 'unconfirmed')
        writes = deepcopy(self.access.writes)
        self.access.client.request = original
        result = self.run_probe(apply=True, receipt_id=qualified['receipt_id'], check_workflow=True)
        self.assertEqual(result['workflow_checks']['without_workflows']['status'], 'accepted')
        self.assertEqual(self.access.writes, writes)

    def test_workflow_positive_case_requests_only_already_granted_permission(self):
        qualified = self.run_probe(apply=True)
        self.access.permissions['workflows'] = 'write'
        self.access.connection = self.access.conn
        self.access.mint = Mock(return_value='workflow-token')
        result = self.run_probe(apply=True, receipt_id=qualified['receipt_id'], check_workflow=True)
        self.assertEqual(result['positive_test'], 'observed')
        self.assertEqual(result['workflow_checks']['with_existing_grant']['status'], 'accepted')
        self.access.mint.assert_called_once_with(self.access.conn, [2], write=True, workflows=True)
        self.assertEqual(self.access.scratch_token, 'private-token')
        self.assertFalse(any('dispatches' in suffix for _, suffix, _ in self.access.writes))

    def test_cli_has_plan_apply_and_receipt_without_secret_arguments(self):
        args = parser().parse_args(['setup','qualify-publication','--ssh','anvil','--repo','owner/target','--probe-repo','owner/scratch'])
        run = Mock(return_value=SimpleNamespace(returncode=0,stdout=b'{"status":"planned"}'))
        self.assertEqual(execute(args, run=run)['status'], 'planned')
        payload = json.loads(run.call_args.kwargs['input'])
        self.assertEqual(set(payload), {'repo','probe_repo','target_branch','probe_ref','apply','receipt','check_workflow','service_user','source','root'})
        self.assertFalse(payload['apply'])
        self.assertEqual(payload['service_user'], 'forge')
        self.assertEqual(payload['source'], '/home/forge/forge')
        command = run.call_args.args[0]
        self.assertIn('StrictHostKeyChecking=yes', command)
        self.assertIn("forge.launch", command[-1])

    def test_cli_accepts_existing_forge_account_and_explicit_paths(self):
        args = parser().parse_args(['setup','qualify-publication','--ssh','forge','--repo','owner/target','--probe-repo','owner/scratch',
            '--service-user','luc','--source','/home/luc/forger-remote','--root','/home/luc/ops/forge-watchdog'])
        run = Mock(return_value=SimpleNamespace(returncode=0,stdout=b'{"status":"planned"}'))
        execute(args, run=run)
        payload = json.loads(run.call_args.kwargs['input'])
        self.assertEqual(payload['service_user'], 'luc')
        self.assertEqual(payload['source'], '/home/luc/forger-remote')
        self.assertEqual(payload['root'], '/home/luc/ops/forge-watchdog')
        command = run.call_args.args[0]
        self.assertIn('luc', command[-1])
        for key,value in [('service_user','luc; command'),('source','relative'),('root','/tmp/../etc')]:
            original = getattr(args,key);setattr(args,key,value)
            with self.assertRaises(SetupError):execute(args,run=run)
            setattr(args,key,original)
        self.assertEqual(run.call_count, 1)

    def test_host_runs_selected_checkout_as_current_service_account_and_requires_module(self):
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve();source=base/'source';root=base/'runtime'
            source.mkdir();root.mkdir()
            payload=dict(source=str(source),root=str(root),service_user='luc',repo='owner/target',probe_repo='owner/scratch')
            account=SimpleNamespace(pw_uid=os.geteuid(),pw_name='luc',pw_dir=str(base))
            def host():
                stdout=io.StringIO()
                with patch('sys.stdin',SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode()))),patch('sys.stdout',stdout),patch('pwd.getpwuid',return_value=account):
                    with self.assertRaises(SystemExit) as stopped:exec(compile(HOST,'<host>','exec'),{})
                    self.assertEqual(stopped.exception.code,0)
                return json.loads(stdout.getvalue())
            with patch('subprocess.run') as child:
                self.assertEqual(host()['reason'],'qualification_module_not_installed')
                child.assert_not_called()
            (source/'forge').mkdir()
            for name in ('launch.py','publication_qualification.py'):(source/'forge'/name).touch()
            with patch('subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=b'{"status":"planned"}')) as child:
                self.assertEqual(host()['status'],'planned')
                self.assertEqual(child.call_args.args[0],['/usr/bin/python3','-m','forge.launch','forge.publication_qualification','--root',str(root)])
                self.assertEqual(child.call_args.kwargs['cwd'],source)
                self.assertEqual(child.call_args.kwargs['env']['HOME'],str(base))
                self.assertNotIn('source',json.loads(child.call_args.kwargs['input']))



class TokenScopeTests(unittest.TestCase):
    def test_target_token_is_read_only_and_only_scratch_token_can_write(self):
        from forge.publication_qualification import Access as HostAccess
        requests = []
        connection = dict(app_id=4, installation_id=5, private_key_env='APP_KEY')
        def request(method, path, token, payload=None):
            requests.append((method, path, token, payload))
            if path == '/app/installations/5':
                return dict(id=5, app_id=4, account={'id':7}, permissions={'contents':'write','administration':'read'})
            if path.endswith('/access_tokens'):
                return {'token':'secret-' + str(len(requests)), 'permissions':dict(payload['permissions'])}
            if path in ('/repos/owner/target', '/repos/owner/scratch'):
                name = path.removeprefix('/repos/')
                return dict(id=1 if name.endswith('target') else 2, full_name=name, private=True,
                            owner={'id':7}, default_branch='main', node_id='node')
            if path == '/app': return {'id':4,'slug':'controller'}
            if path == '/users/controller%5Bbot%5D': return {'id':9,'login':'controller[bot]','type':'Bot'}
            if path == '/installation/token': return {}
            self.fail(path)
        client = SimpleNamespace(request=request, jwt=lambda value:'app-jwt')
        settings = SimpleNamespace(client=client, load=lambda:dict(connections={'controller':connection},
            repositories=[dict(id=1,name='owner/target',enabled=False)]))
        access = HostAccess(settings)
        access.open('owner/target','owner/scratch',write=True)
        mints = [payload for method,path,token,payload in requests if path.endswith('/access_tokens')]
        self.assertEqual([p for p in mints if p['permissions'].get('contents') == 'write'],
                         [{'repository_ids':[2],'permissions':{'contents':'write','metadata':'read','administration':'read'}}])
        self.assertTrue(any(p.get('repository_ids') == [1] and p['permissions']['contents'] == 'read' for p in mints))
        with self.assertRaisesRegex(QualificationError,'workflows_grant_missing'):
            access.mint(connection,[2],write=True,workflows=True)
        access.close()
        self.assertEqual(sum(path == '/installation/token' for _,path,_,_ in requests),len(mints))
