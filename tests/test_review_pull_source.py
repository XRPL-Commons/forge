"""Review external PR heads only through their authorized base repository."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forge.mirror.git_store import GitStore, environment, run_git
from forge.mirror.policy import MirrorError, Repository
from tests.test_mirror_service import no_credentials


class PullHeadFixture:
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        self.root=Path(directory.name);self.remote=self.root/'remote.git'
        run_git(['init','--bare',str(self.remote)])
        env={**environment(),'GIT_AUTHOR_NAME':'Fixture','GIT_AUTHOR_EMAIL':'fixture@example.test',
             'GIT_COMMITTER_NAME':'Fixture','GIT_COMMITTER_EMAIL':'fixture@example.test'}
        tree=run_git(['--git-dir',str(self.remote),'mktree'],data=b'').decode().strip()
        self.base=run_git(['--git-dir',str(self.remote),'commit-tree',tree,'-m','base'],env=env).decode().strip()
        self.head=run_git(['--git-dir',str(self.remote),'commit-tree',tree,'-p',self.base,'-m','fork head'],env=env).decode().strip()
        run_git(['--git-dir',str(self.remote),'update-ref','refs/heads/main',self.base])
        run_git(['--git-dir',str(self.remote),'update-ref','refs/pull/17/head',self.head])
        self.cache=GitStore(self.root/'cache',credentials=no_credentials)
        self.repository=Repository(1,'acme/project',10,'Organization',20,default_branch='main',actual_access=True)
        self.fetches=[]
        def local_git(args,**kwargs):
            if 'fetch' in args:
                self.fetches.append(list(args))
                self.assertIn('https://github.com/acme/project.git',args)
                args=[str(self.remote) if arg=='https://github.com/acme/project.git' else arg for arg in args]
                kwargs['env']={**kwargs.get('env',{}),'GIT_ALLOW_PROTOCOL':'file'}
            return run_git(args,**kwargs)
        self.addCleanup(patch.stopall)
        patch('forge.mirror.git_store.run_git',side_effect=local_git).start()


class PullHeadFetchTests(PullHeadFixture,unittest.TestCase):
    def test_fork_head_uses_base_pull_ref_and_exact_head_pin(self):
        result=self.cache.ensure(self.repository,pinned_oids=[self.base,self.head],pull_head={'number':17,'oid':self.head})
        self.assertTrue(result['host_fetch'])
        self.assertTrue(any('+refs/pull/17/head:refs/forge/pull-heads/17' in args for args in self.fetches))
        self.assertFalse(any(args[-1]==self.head for args in self.fetches))
        pinned=run_git(['--git-dir',str(self.cache.path(1)),'rev-parse','refs/forge/pins/'+self.head]).decode().strip()
        self.assertEqual(pinned,self.head)

    def test_changed_pull_ref_cannot_authorize_a_different_cached_head(self):
        self.cache.ensure(self.repository,pinned_oids=[self.base,self.head],pull_head={'number':17,'oid':self.head})
        run_git(['--git-dir',str(self.remote),'update-ref','refs/pull/17/head',self.base])
        with self.assertRaisesRegex(MirrorError,'assignment_changed'):
            self.cache.ensure(self.repository,pinned_oids=[self.base,self.head],pull_head={'number':17,'oid':self.head})

    def test_unbounded_or_unpinned_pull_identity_never_fetches(self):
        for value in ({'number':True,'oid':self.head},{'number':0,'oid':self.head},
                      {'number':17,'oid':self.head,'ref':'refs/heads/other'}, {'number':17,'oid':self.base}):
            with self.subTest(value=value),self.assertRaises(MirrorError):
                self.cache.ensure(self.repository,pinned_oids=[self.head],pull_head=value)
        self.assertEqual(self.fetches,[])


class ReviewOnlyInputTests(PullHeadFixture,unittest.TestCase):
    def setUp(self):
        super().setUp()
        from forge.store.db import Store
        from forge.store.executions import Executions
        from forge.store.review_pulls import ReviewPullRequests, normalize_source
        self.store=Store.create(self.root/'database')
        self.life=Executions(self.store,clock=lambda:100)
        with self.store.write() as db:
            db.execute("INSERT INTO repositories(id,full_name) VALUES(1,'acme/project')")
        remote=dict(number=17,title='Fix the public API',body='Preserve the documented response shape.',
                    user=dict(id=44,login='contributor',type='User'),state='open',draft=False,merged=False,labels=[],
                    head=dict(sha=self.head,ref='contribution',repo=dict(id=2,full_name='contributor/fork')),
                    base=dict(sha=self.base,ref='main',repo=dict(id=1,full_name='acme/project')))
        self.source=normalize_source(remote,1,'acme/project')
        self.admission=ReviewPullRequests(self.store,clock=lambda:100)
        receipt=self.admission.observe(self.source,policy_hash='policy')
        self.unit=receipt['unit_id'];self.candidate=receipt['candidate_id']

    def reserve(self,role='review'):
        from forge.store.models import Command
        from forge.store.executions import ExecutionQueries,Launch
        with self.store.read_snapshot() as db:
            candidate=dict(db.execute('SELECT * FROM candidates WHERE id=?',(self.candidate,)).fetchone())
            version=db.execute('SELECT version FROM units WHERE id=?',(self.unit,)).fetchone()[0]
        # The candidate-effect controller separately proves the pending Review/Audit check.
        with self.store.write() as db:
            gate=db.execute("SELECT id FROM unit_gates WHERE unit_id=? AND resolved_at IS NULL AND "
                "json_extract(details_json,'$.source')='review_pull_policy'",(self.unit,)).fetchone()
            if gate:
                self.life._resolve(db,Command('confirm-'+gate[0],'supervisor:effects',self.unit,version),
                                   gate[0],{'confirmed':True})
            if role=='audit':db.execute("UPDATE units SET state='auditing' WHERE id=?",(self.unit,))
        manifest={key:candidate[key] for key in ('head_oid','base_oid','policy_hash','requirements_hash','input_manifest_hash')}
        manifest['candidate_id']=self.candidate
        receipt=self.life.reserve_job(Command('reserve-'+role,'operator',self.unit,version),role,
            slot=role,execution_fence='fence-'+role,input_manifest=manifest,candidate_id=self.candidate,
            launch=Launch('contribution','forge[bot]','codex',1000,{},{}))
        return ExecutionQueries(self.store).job(receipt['job_id'])

    def preparation(self):
        from forge.factory.source_preparation import SourcePreparation
        from tests.test_source_preparation import Settings,Access
        from unittest.mock import Mock
        self.access=Access();self.github=Mock()
        return SourcePreparation(self.root,self.store,settings=Settings(),access=self.access,
                                 git_store=self.cache,github=self.github,clock=lambda:100)

    def test_memberless_review_freezes_exact_pull_intent_and_fork_head(self):
        import json
        from forge.factory.contracts import assignment_note
        job=self.reserve();note=assignment_note(job)
        self.assertEqual(job['members'],[])
        self.assertTrue(job['review_only'])
        self.assertEqual(job['review_source'],self.source)
        self.assertIsNone(job['issue_number'])
        self.assertNotIn('members',note);self.assertNotIn('issue_number',note)
        preparation=self.preparation()
        prepared=preparation.prepare(job,note)
        frozen=json.loads((prepared.input_directory/'assignment.json').read_text())
        self.assertEqual(frozen['review_source'],self.source)
        self.assertNotIn('issue_observations',frozen)
        self.assertEqual(prepared.source_input['input_oids'],[self.head])
        self.assertEqual(prepared.source_input['base_oid'],self.base)
        self.assertEqual(prepared.publication_authorization,{})
        self.assertEqual(self.access.calls,['acme/project'])
        self.github.issue_inputs.assert_not_called()
        with self.store.read_snapshot() as db:
            for table in ('issues','unit_issues','job_members','unit_builders'):
                self.assertEqual(db.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
        replay=preparation.prepare(job,{**note,'review_source':{'title':'replacement'}})
        self.assertEqual(replay.source_input,prepared.source_input)
        self.assertEqual(json.loads((replay.input_directory/'assignment.json').read_text()),frozen)

    def test_memberless_audit_uses_same_candidate_and_cannot_publish(self):
        from forge.factory.contracts import assignment_note
        job=self.reserve('audit');note=assignment_note(job)
        self.assertEqual(note['candidate']['key'],self.candidate)
        self.assertEqual(note['review_source']['author'],self.source['author'])
        self.assertIn('independently evaluates an existing pull request',note['instructions'])
        with self.assertRaisesRegex(MirrorError,'assignment_changed'):
            self.preparation().prepare(job,note,publication_authorization={'branch':'contribution'})

    def test_changed_source_revokes_previous_candidate_mirror(self):
        from forge.factory.contracts import assignment_note
        job=self.reserve();preparation=self.preparation()
        prepared=preparation.prepare(job,assignment_note(job))
        session=preparation.mirror.session(prepared.session_id)
        self.admission.observe({**self.source,'title':'Different intent'},policy_hash='policy')
        self.assertFalse(preparation.mirror.live_authority(session))
        with self.assertRaisesRegex(MirrorError,'session_revoked'):
            preparation.prepare(job,assignment_note(job))
