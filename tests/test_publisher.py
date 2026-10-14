from copy import deepcopy
import base64
import hashlib
import unittest
from urllib.parse import unquote
from types import SimpleNamespace

from forge.factory.github import GitHubUnavailable
from forge.factory.publisher import Publisher
from forge.publication_artifacts import Change, VerifiedOutput
from tests import test_publications as ledger


class GitHub:
    def __init__(self):
        self.ref=None;self.commit=None;self.pull=None;self.writes=[];self.extras=[];self.lose=set();self.reject={};self.verified=True
        self.read_error=None;self.base=ledger.BASE;self.tree='d'*40
        self.client=SimpleNamespace(jwt=lambda connection:'synthetic-app-jwt',request=lambda method,path,token:{'id':43,'app_id':42,'permissions':{'contents':'write','pull_requests':'write'}})
        self.bot='controller[bot]';self.head='e'*40;self.contains_base=True
    def connection(self,*args):return {'app_id':42,'installation_id':43}
    def publisher(self,*args):return self.bot
    def contains(self,*args):return self.contains_base
    def mutation(self,key,value):
        if key in self.reject:raise GitHubUnavailable('redacted',status=self.reject[key])
        if key in self.lose:self.lose.remove(key);raise GitHubUnavailable('lost',status=500)
        return value
    def call(self,role,job,method,path,payload=None,**kwargs):
        if kwargs.get('extra'):self.extras.append(kwargs['extra'])
        if method!='GET':self.writes.append((method,path,deepcopy(payload)))
        branch=job['branch']
        if path=='':return {'id':1,'node_id':'R_1'}
        if path.startswith('/git/ref/'):
            if self.ref is None:raise GitHubUnavailable('missing',status=404)
            return {'ref':'refs/heads/'+branch,'object':{'type':'commit','sha':self.ref}}
        if path=='/git/blobs' and method=='POST':
            content=base64.b64decode(payload['content']);return {'sha':hashlib.sha1(b'blob '+str(len(content)).encode()+b'\0'+content).hexdigest()}
        if path=='/git/trees' and method=='POST':
            raw=b''.join((entry['mode'].lstrip('0')+' '+entry['path']).encode()+b'\0'+bytes.fromhex(entry['sha']) for entry in payload['tree'])
            return {'sha':hashlib.sha1(b'tree '+str(len(raw)).encode()+b'\0'+raw).hexdigest()}
        if path=='/git/commits' and method=='POST':
            self.commit={'sha':self.head,'tree':{'sha':payload['tree']},'parents':[{'sha':p} for p in payload['parents']],
                'message':payload['message'],'verification':{'verified':self.verified}}
            return self.mutation('commit',{'sha':self.head})
        if path.startswith('/git/commits/'):
            if self.read_error:raise GitHubUnavailable('read unavailable',status=self.read_error)
            return deepcopy(self.commit)
        if path.startswith('/commits/'):return {'sha':self.head,'author':{'id':42,'login':self.bot,'type':'Bot'}}
        if path.startswith('/pulls?'):return [] if self.pull is None else [deepcopy(self.pull)]
        if path=='/pulls' and method=='POST':
            self.pull={'number':7,'state':'open','draft':False,'merged':False,'body':payload['body'],
                'head':{'sha':self.ref,'ref':branch,'repo':{'id':1}},'base':{'sha':self.base,'ref':'main','repo':{'id':1}},
                'user':{'id':42,'login':self.bot,'type':'Bot'}}
            return self.mutation('pr',deepcopy(self.pull))
        if path.startswith('/pulls/'):
            if method=='PATCH':self.pull['body']=payload['body']
            value=deepcopy(self.pull);value['head']['sha']=self.ref
            return value
        raise AssertionError((method,path))
    def graphql(self,role,job,query,variables,**kwargs):
        if kwargs.get('extra'):self.extras.append(kwargs['extra'])
        self.writes.append(('GRAPHQL',query,deepcopy(variables)))
        value=variables['input']
        if 'refUpdates' in value:
            update=value['refUpdates'][0]
            if update['beforeOid']!=(self.ref or '0'*40):raise GitHubUnavailable('CAS',status=200)
            self.ref=update['afterOid'];return self.mutation('ref',{'updateRefs':{'clientMutationId':None}})
        self.assert_commit(value)
        self.ref=self.head
        self.commit={'sha':self.head,'tree':{'sha':self.tree},'parents':[{'sha':value['expectedHeadOid']}],
            'message':value['message']['headline']+'\n\n'+value['message']['body'],'verification':{'verified':self.verified}}
        return self.mutation('commit',{'createCommitOnBranch':{'commit':{'oid':self.head}}})
    def assert_commit(self,value):
        assert value['expectedHeadOid']==self.ref
        assert value['branch']['branchName']


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.fixture=ledger.PublicationLedgerTests()
        self.configure()

    def configure(self):
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        f=self.fixture;self.github=GitHub();self.capability={'app_id':42,'strategy':'rest','qualified':True}
        self.publisher=Publisher(f.store,self.github,lambda job:f.output,signing_capability=lambda job:self.capability,
            clock=lambda:f.now,reliability={'effect_uncertainty_seconds':90},max_mutations=1)
        self.identifier=None
    def visit(self):
        f=self.fixture
        if self.identifier is None:
            result=self.publisher.ensure(self.publisher.jobs.job(f.job),f.output,f.target,f.report)
            self.identifier=result.get('intent_id')
        else:result=self.publisher.step(self.identifier)
        f.now+=31
        return result
    def finish(self):
        for _ in range(15):
            result=self.visit()
            if result['status'] in ('confirmed','blocked','deferred'):return result
        self.fail('publication did not settle')
    def count(self,path):return sum(call[1]==path for call in self.github.writes)

    def test_rest_exact_commit_ref_pr_receipt_and_replay(self):
        result=self.finish();self.assertEqual(result['status'],'confirmed',result)
        self.assertEqual(result['receipt']['head_oid'],self.github.head)
        self.assertEqual(self.count('/git/commits'),1);self.assertEqual(self.count('/pulls'),1)
        self.assertEqual(self.visit()['receipt'],result['receipt'])
        payload=next(call[2] for call in self.github.writes if call[1]=='/git/commits')
        self.assertNotIn('author',payload);self.assertNotIn('committer',payload)
        cas=next(call[2]['input']['refUpdates'][0] for call in self.github.writes if call[0]=='GRAPHQL')
        self.assertEqual(cas['beforeOid'],'0'*40);self.assertFalse(cas['force'])

    def test_lost_rest_commit_is_never_blindly_repeated(self):
        self.github.lose.add('commit');result=self.finish()
        self.assertEqual(result['status'],'blocked');self.assertEqual(result['claim']['code'],'publication_uncertain')
        self.assertEqual(self.count('/git/commits'),1);self.assertIsNone(self.github.ref)

    def test_lost_ref_and_pr_response_recover_by_exact_readback(self):
        self.github.lose.update({'ref','pr'});result=self.finish()
        self.assertEqual(result['status'],'confirmed',result)
        self.assertEqual(self.count('/pulls'),1)
        self.assertEqual(sum(call[0]=='GRAPHQL' for call in self.github.writes),1)

    def test_unsigned_commit_cannot_change_ref(self):
        self.github.verified=False;result=self.finish()
        self.assertEqual(result['claim']['code'],'publication_commit_unverified');self.assertIsNone(self.github.ref)

    def test_changed_branch_is_not_overwritten(self):
        self.visit();self.github.ref='f'*40
        result=self.finish();self.assertEqual(result['claim']['code'],'publication_head_changed')
        self.assertEqual(self.github.ref,'f'*40)

    def test_unknown_signing_capability_prevents_any_write(self):
        self.capability['qualified']=False
        result=self.visit();self.assertEqual(result['claim']['code'],'publication_signing_unqualified')
        self.assertEqual(self.github.writes,[])

    def test_revoked_admission_prevents_next_write(self):
        self.visit();self.publisher.admission=lambda job:False
        result=self.visit();self.assertEqual(result['status'],'deferred');self.assertIsNone(self.github.ref)

    def test_graphql_lost_commit_recovers_tree_signature_and_controller_identity(self):
        self.capability['strategy']='graphql';self.fixture.target['strategy']='graphql';self.github.lose.add('commit')
        result=self.finish();self.assertEqual(result['status'],'confirmed',result)
        self.assertEqual(sum('CreateCommitOnBranchInput' in call[1] for call in self.github.writes),1)

    def test_graphql_special_mode_refused_before_creating_branch(self):
        f=self.fixture;self.capability['strategy']='graphql';f.target['strategy']='graphql'
        f.output=VerifiedOutput(f.output.source,f.output.bundle_digest,7,ledger.HEAD,'c'*40,'d'*40,
            (Change('run',None,None,'100755','a'*40,b'x'),),{})
        result=self.finish();self.assertEqual(result['claim']['code'],'file_mode_unsupported');self.assertEqual(self.github.writes,[])

    def test_http403_is_not_asserted_to_be_signing_rule_rejection(self):
        self.github.reject['commit']=403
        result=self.finish();self.assertEqual(result['claim'],{'code':'publication_refused','step':'commit','http_status':403})

    def rework(self,*,replace=False,graphql=False):
        self.fixture.doCleanups();self.fixture=ledger.PublicationLedgerTests();self.fixture.prior_head='f'*40
        self.fixture.replace_ref=replace;self.configure()
        self.github.ref='f'*40
        self.github.pull={'number':7,'state':'open','draft':False,'merged':False,'body':'Human context remains.',
            'head':{'sha':'f'*40,'ref':self.fixture.target['branch'],'repo':{'id':1}},
            'base':{'sha':ledger.BASE,'ref':'main','repo':{'id':1}},'user':{'id':99,'login':'builder[bot]','type':'Bot'}}
        if graphql:self.fixture.target['strategy']='graphql';self.capability['strategy']='graphql'

    def test_rest_rework_preserves_pr_author_and_human_body(self):
        self.rework();result=self.finish()
        self.assertEqual(result['status'],'confirmed',result)
        self.assertEqual(result['receipt']['pr_author']['login'],'builder[bot]')
        self.assertTrue(self.github.pull['body'].startswith('Human context remains.'))
        self.assertIn('Details',self.github.pull['body']);self.assertEqual(self.count('/pulls'),0)
        self.assertEqual(self.github.commit['parents'],[{'sha':'f'*40}])

    def test_rest_moved_base_replaces_only_explicitly_authorized_target(self):
        self.rework(replace=True);self.github.contains_base=False
        result=self.finish();self.assertEqual(result['status'],'confirmed',result)
        self.assertEqual(self.github.commit['parents'],[{'sha':ledger.BASE}])
        cas=next(call[2]['input']['refUpdates'][0] for call in self.github.writes if call[0]=='GRAPHQL')
        self.assertEqual(cas['beforeOid'],'f'*40);self.assertTrue(cas['force'])

    def test_moved_base_graphql_never_resets_existing_branch(self):
        self.rework(replace=True,graphql=True);self.github.contains_base=False
        result=self.finish();self.assertEqual(result['claim']['code'],'publication_base_changed')
        self.assertEqual(self.github.writes,[]);self.assertEqual(self.github.ref,'f'*40)

    def test_closed_existing_pr_prevents_ref_or_commit_mutation(self):
        self.rework();self.github.pull['state']='closed'
        result=self.finish();self.assertEqual(result['claim']['code'],'publication_pr_changed');self.assertEqual(self.github.writes,[])

    def test_normal_publication_completes_in_one_bounded_visit(self):
        self.publisher.max_mutations=16
        result=self.visit();self.assertEqual(result['status'],'confirmed',result)
        self.assertEqual(len(self.github.writes),3)

    def test_readback_outage_has_bounded_owned_exit(self):
        self.github.read_error=503;result=self.finish()
        self.assertEqual(result['status'],'blocked');self.assertEqual(result['claim']['code'],'publication_uncertain')
        self.assertTrue(result['claim']['step'].startswith('read:'));self.assertEqual(self.count('/git/commits'),1)

    def test_readback_outage_recovers_without_repeating_commit(self):
        self.github.read_error=503;self.visit();self.github.read_error=None
        result=self.finish();self.assertEqual(result['status'],'confirmed',result)
        self.assertEqual(self.count('/git/commits'),1)

    def test_definite_rate_limit_refusal_retries_after_backoff(self):
        self.github.reject['commit']=429
        self.assertEqual(self.visit()['status'],'uncertain')
        del self.github.reject['commit']
        result=self.finish();self.assertEqual(result['status'],'confirmed',result)
        self.assertEqual(self.count('/git/commits'),2)

    def test_workflow_scope_is_requested_only_when_already_granted(self):
        from dataclasses import replace
        self.fixture.output=replace(self.fixture.output,facts={'protected_paths':['.github/workflows/ci.yml']})
        self.capability['workflows_granted']=True
        self.assertEqual(self.finish()['status'],'confirmed')
        self.assertTrue(self.github.extras)
        self.assertTrue(all(value=={'workflows':'write'} for value in self.github.extras))

    def test_ungranted_workflow_scope_is_never_requested(self):
        from dataclasses import replace
        self.fixture.output=replace(self.fixture.output,facts={'protected_paths':['.github/workflows/ci.yml']})
        self.capability['workflows_granted']=False
        self.assertEqual(self.finish()['status'],'confirmed')
        self.assertEqual(self.github.extras,[])
