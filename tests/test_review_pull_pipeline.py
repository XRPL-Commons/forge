"""Composed human/fork PR review with real durable controllers and fake remotes."""
from copy import deepcopy
import json
import tempfile
import unittest

from forge.factory.config import policy, validate
from forge.factory.contracts import digest
from forge.factory.dispatch_controller import DispatchController, GitHubPrepare
from forge.factory.effect_controller import EffectController
from forge.factory.execution_controller import ExecutionController
from forge.factory.merge_checks import ReviewPullsController
from forge.factory.merge_controller import MergeAdmission, MergeController
from forge.factory.observation_controller import ObservationController
from forge.factory.result_controller import ResultController
from forge.factory.sqlite_supervisor import SqliteSupervisor
from forge.store.db import Store
from forge.store.queries import Queries
from tests.test_store_sqlite_supervisor import GitHub, Runner, HEAD, BASE


class HumanPullGitHub(GitHub):
    def __init__(self):
        super().__init__()
        self.pull = dict(number=7,state='open',draft=False,merged=False,mergeable=True,
            title='Preserve the response shape',body='A human contribution without a linked issue.',labels=[],
            user={'id':44,'login':'contributor','type':'User'},
            head={'sha':HEAD,'ref':'human-change','repo':{'id':2,'full_name':'contributor/fork'}},
            base={'sha':BASE,'ref':'main','repo':{'id':1,'full_name':'owner/repo'}})
        self.published=[]

    def issue(self,item):
        raise AssertionError('Human PR review must not invent an issue')

    def call(self,role,item,method,path):
        assert method=='GET' and path=='/branches/main'
        return {'commit':{'sha':self.pull['base']['sha']}}

    def verdict(self,job,report):
        assert report['candidate']==job['candidate']['key']
        check_id=100+len(self.published)
        self.published.append({'kind':'verdict','role':job['role'],'outcome':report['outcome'],
                               'head':job['candidate']['head'],'candidate':job['candidate']['key']})
        self.checks[job['role']]=check_id
        self.reviews[job['role']]=job['candidate']['head'] if report['outcome']=='pass' else None
        return check_id

    def check(self,item,role,selected,external_id,conclusion,summary):
        assert role=='controller'
        self.published.append({'kind':'policy','conclusion':conclusion,'head':selected['head'],
                               'candidate':selected['key'],'external_id':external_id})
        return 100+len(self.published)

    def publish(self,job):
        raise AssertionError('Human PR must never invoke a builder publication')

    def merge(self,item):
        self.merge_calls+=1
        raise AssertionError('Human PR review grants no merge authority')


class HumanPullRunner(Runner):
    def __init__(self,github):
        super().__init__(github)
        self.outcomes={}
        self.assignments=[]
        self.hold=False

    def start(self,job):
        assert job['role'] in {'review','audit'} and job['members']==[] and job['review_only']
        self.assignments.append(deepcopy(job))
        super().start(job)
        if self.hold:self.containers[job['id']].update(status='running',running=True)

    def capture(self,job):
        result=super().capture(job)
        result['result']['outcome']=self.outcomes.get(job['role'],'pass')
        return result


class ReviewPullPipelineTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        self.store=Store.create(directory.name);self.now=100.0
        with self.store.write() as db:
            db.execute("INSERT INTO repositories(id,full_name) VALUES(1,'owner/repo')")
        self.github=HumanPullGitHub();self.runner=HumanPullRunner(self.github)
        self.config=validate(dict(enabled=True,merge_enabled=True,human_required=False,image='sha256:'+'1'*64))
        self.unit=None

    def admission_controller(self):
        return ReviewPullsController(self.store,self.github,lambda:self.config,clock=lambda:self.now)

    def discover(self):
        results=self.admission_controller().reconcile({'repo_id':1,'repo':'owner/repo'},self.github.pulls({}))
        if results:self.unit=results[0]['unit_id']
        return results

    def driver(self):
        clock=lambda:self.now
        admission=MergeAdmission(self.store,self.github,config=lambda:self.config,repository_ready=lambda repo:True,clock=clock)
        effects=EffectController(self.store,self.github,admission=admission,clock=clock)
        return SqliteSupervisor(self.store,
            execution=ExecutionController(self.store,self.runner,self.github,enabled=lambda:True,admission=lambda job:True,clock=clock),
            observations=ObservationController(self.store,self.github,admission=lambda item:True,
                policy=lambda item:digest(policy(self.config,1,repository_name='owner/repo')),clock=clock),
            review_pulls=self.admission_controller(),
            results=ResultController(self.store,self.github,policy=lambda job:policy(self.config,1,repository_name='owner/repo'),verdicts=effects,clock=clock),
            merges=MergeController(self.store,admission=admission,clock=clock),effects=effects,
            dispatch=DispatchController(self.store,self.runner,config=self.config,prepare=GitHubPrepare(self.github,self.config),
                admission=lambda snapshot:True,clock=clock),clock=clock)

    def cycle(self):
        self.now+=60
        return self.driver().cycle(limit=50)

    def complete(self):
        results=[]
        for _ in range(8):
            results.append(self.cycle())
            if self.successes():return
        self.fail('Review/Audit did not publish policy success: '+repr(results))

    def successes(self):
        return [check for check in self.github.published if check['kind']=='policy' and check['conclusion']=='success']

    def assert_no_builder_or_merge(self):
        self.assertEqual(self.github.merge_calls,0)
        with self.store.read_snapshot() as db:
            for table in ('issues','unit_issues','job_members','unit_builders','merge_requests'):
                self.assertEqual(db.execute('SELECT count(*) FROM '+table).fetchone()[0],0,table)
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE role='build'").fetchone()[0],0)

    def test_human_fork_review_audit_publish_one_durable_policy_without_merge(self):
        self.discover();self.complete()
        self.assertEqual(self.runner.launched,['review','audit'])
        self.assertEqual(self.runner.cleaned,self.runner.launched)
        self.assertEqual(self.runner.containers,{})
        self.assertEqual(len(self.successes()),1)
        self.assertEqual([entry['kind'] for entry in self.github.published],['policy','verdict','verdict','policy'])
        self.assertIsNone(self.github.published[0]['conclusion'])
        self.assertEqual(self.runner.assignments[0]['review_source']['author']['login'],'contributor')
        with self.store.read_snapshot() as db:
            row=db.execute("SELECT status,receipt_json FROM external_effects WHERE kind='github_check' AND json_extract(payload_json,'$.review_only')=1").fetchone()
            self.assertEqual(row['status'],'confirmed');self.assertIsInstance(json.loads(row['receipt_json'])['check_id'],int)
        for _ in range(3):self.cycle()
        self.assertEqual(self.runner.launched,['review','audit']);self.assertEqual(len(self.successes()),1)
        self.assert_no_builder_or_merge()

    def test_changed_head_or_base_requires_new_independent_review_and_audit(self):
        for part,new_sha in [('head','d'*40),('base','e'*40)]:
            with self.subTest(part=part):
                self.discover();self.complete()
                previous=Queries(self.store).unit(self.unit)['current_candidate_id']
                before=len(self.successes());started=len(self.runner.launched)
                self.github.pull[part]['sha']=new_sha
                self.cycle()
                self.assertEqual(len(self.successes()),before)
                current=Queries(self.store).unit(self.unit)['current_candidate_id']
                self.assertNotEqual(current,previous)
                for _ in range(8):
                    self.cycle()
                    if len(self.successes())>before:break
                self.assertEqual(len(self.successes()),before+1)
                self.assertEqual(self.runner.launched[started:],['review','audit'])
                self.assertEqual(self.successes()[-1]['candidate'],current)
                self.assertEqual(self.successes()[-1]['head'],self.github.pull['head']['sha'])
                self.assert_no_builder_or_merge()

    def test_head_and_base_changes_fence_running_review_before_replacement(self):
        self.runner.hold=True;self.discover()
        self.cycle();self.cycle()
        self.assertEqual(self.runner.launched,['review'])
        for part,new_sha in [('head','d'*40),('base','e'*40)]:
            previous_job=self.runner.assignments[-1]['id']
            self.github.pull[part]['sha']=new_sha
            self.cycle()
            with self.store.read_snapshot() as db:
                job=db.execute('SELECT fenced FROM jobs WHERE id=?',(previous_job,)).fetchone()
                self.assertTrue(job['fenced'])
            self.assertEqual(self.successes(),[])
            self.cycle();self.cycle()
            self.assertNotEqual(self.runner.assignments[-1]['id'],previous_job)
            self.assertEqual(self.runner.assignments[-1]['candidate']['head'],self.github.pull['head']['sha'])
            self.assertEqual(self.runner.assignments[-1]['candidate']['base'],self.github.pull['base']['sha'])
        self.runner.hold=False
        active=self.runner.assignments[-1]['id']
        self.runner.containers[active].update(status='exited',running=False)
        self.complete()
        self.assertEqual(self.runner.launched,['review','review','review','audit'])
        self.assertEqual(len(self.successes()),1)
        self.assertEqual([item['outcome'] for item in self.github.published if item['kind']=='verdict'],['pass','pass'])
        self.assert_no_builder_or_merge()

    def test_rejected_review_waits_for_human_revision_without_builder(self):
        self.runner.outcomes['review']='reject';self.discover()
        for _ in range(6):self.cycle()
        self.assertEqual(self.runner.launched,['review']);self.assertEqual(self.successes(),[])
        with self.store.read_snapshot() as db:
            self.assertIsNotNone(db.execute("SELECT id FROM unit_gates WHERE unit_id=? AND resolved_at IS NULL AND code='review_source'",(self.unit,)).fetchone())
        self.runner.outcomes.clear();self.github.pull['head']['sha']='d'*40
        self.complete()
        self.assertEqual(self.runner.launched,['review','review','audit'])
        self.assert_no_builder_or_merge()

    def test_forge_only_scope_excludes_human_pr(self):
        self.config['repository_merge_check_scopes']={'1':'forge'}
        self.assertEqual(self.discover(),[])
        for _ in range(3):self.cycle()
        self.assertEqual(self.runner.launched,[]);self.assertEqual(self.github.published,[])
        with self.store.read_snapshot() as db:self.assertEqual(db.execute('SELECT count(*) FROM units').fetchone()[0],0)
        self.assert_no_builder_or_merge()
