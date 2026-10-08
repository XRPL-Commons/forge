from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
import uuid
from unittest.mock import Mock, patch

from forge.factory.config import validate
from forge.factory.discussions import Discussions, note
from forge.factory.sqlite_discussions import SQLiteDiscussions
from forge.factory.runner import RunnerError
from forge.store.db import Store
from forge.store.executions import Executions, provider_available
from forge.store.models import IdempotencyConflict, TransitionError


class DiscussionTests(unittest.TestCase):
    def setUp(self):
        directory=TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.root=Path(directory.name); self.store=Store.create(self.root)
        self.now=1000.0
        Executions(self.store).admit_issue(request_id='admit',actor_id='operator',unit_id='u',issue_id='i',
            repository_id=1,repository_name='example/app',issue_number=2,title='Fix tests',body='Required evidence',requirements='req',observed_at=90)
        self.row=dict(key='1:2',repo='example/app',repo_id=1,number=2,revision='revision-a',title='Fix tests',body='Required evidence',status='failed',actions=['retry'],dependencies=[])
        self.inventory=Mock(); self.inventory.view.side_effect=lambda **_: {'issue':deepcopy(self.row)}
        self.runner=Mock(); self.runner.inspect.return_value={'status':'created','running':False}; self.runner.create.return_value='container-id'
        self.factory=Mock(return_value=self.runner)
        self.service=Discussions(self.root,store=self.store,runner=self.factory,inventory=self.inventory,clock=lambda:self.now,actor_id='github:123')
        self.settings=Mock(); self.settings.paused.return_value=False
        self.github=Mock(); self.github.token.return_value='read-only-token'
        self.config=validate({'enabled':True,'image':'sha256:'+'1'*64})

    def send(self):
        identifier=str(uuid.uuid4()); self.service.send('1:2','Why did this fail?',identifier); return identifier

    def start(self):
        self.send(); self.service.tick(self.config,self.github,self.settings)
        return self.service.read()['1:2']['job']

    def finish(self,job,**fields):
        self.runner.inspect.return_value={'status':'exited','running':False}
        result=dict(attempt=job['id'],role='discussion',outcome='pass',summary='Fix the producer type first.',proposal={'action':'retry','reason':'Fix the producer type first.'}) | fields
        self.runner.capture.return_value={'result':result,'logs':'diagnostic log','execution':{'exit_code':0}}
        self.service.drive(job,self.github)

    def test_explicit_and_selected_routing_use_normalized_rows(self):
        self.assertIsInstance(self.service,SQLiteDiscussions)
        with patch('forge.store.runtime.selected',return_value=self.store):
            self.assertIsInstance(Discussions(self.root,inventory=self.inventory),SQLiteDiscussions)
        identifier=self.send()
        with self.store.read_snapshot() as db:
            message=db.execute('SELECT * FROM messages').fetchone()
            self.assertEqual((message['id'],message['author_id'],message['role']),(identifier,'github:123','operator'))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0],0)
        self.assertFalse((self.root/'state/issues/discussions.json').exists())

    def test_send_idempotent_and_conflicting_identity_rejected(self):
        identifier=self.send()
        self.assertEqual(self.service.send('1:2','Why did this fail?',identifier),identifier)
        self.assertEqual(len(self.service.view('1:2')['messages']),1)
        with self.assertRaises(IdempotencyConflict):
            self.service.send('1:2','A different question',identifier)
        with self.assertRaises(TransitionError):
            self.service.send('1:2','Another question',str(uuid.uuid4()))

    def test_discussion_uses_read_only_token_and_immutable_lease(self):
        job=self.start()
        self.github.token.assert_called_once_with('recovery',1,worker=True)
        self.assertEqual(note(job)['role'],'discussion')
        self.assertEqual(self.service.view('1:2')['status'],'running')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT execution_id FROM discussion_leases').fetchone()[0],job['id'])
            manifest=db.execute('SELECT manifest_json FROM discussion_runtime').fetchone()[0]
            self.assertNotIn('read-only-token',manifest)
        self.assertEqual(job['messages'],[{'role':'user','text':'Why did this fail?'}])

    def test_answer_and_proposal_durable_before_cleanup(self):
        job=self.start(); self.runner.reset.side_effect=RunnerError('cleanup failed')
        self.finish(job)
        view=self.service.view('1:2')
        self.assertEqual(view['messages'][-1]['role'],'assistant')
        self.assertEqual(view['messages'][-1]['proposal']['action'],'retry')
        self.assertTrue(self.service.active_jobs())
        self.runner.reset.side_effect=None
        self.service.drive(job,self.github)
        self.assertFalse(self.service.active_jobs())
        self.assertEqual(len(self.service.view('1:2')['messages']),2)
        self.assertEqual(self.service.view('1:2')['status'],'idle')

    def test_cancel_race_suppresses_answer_and_holds_lease_until_cleanup(self):
        job=self.start()
        self.service.cancel('1:2',actor_id='github:123')
        self.assertTrue(self.service.active_jobs())
        self.finish(job)
        self.assertFalse(any(m['role']=='assistant' for m in self.service.view('1:2')['messages']))
        self.assertFalse(self.service.active_jobs())
        self.assertEqual(self.service.view('1:2')['status'],'cancelled')

    def test_cancel_during_token_lookup_never_starts(self):
        self.send()
        self.github.token.side_effect=lambda *a,**k: self.service.cancel('1:2') or 'token'
        self.service.tick(self.config,self.github,self.settings)
        self.runner.start.assert_not_called()
        self.assertTrue(self.service.active_jobs()[0]['fenced'])
        self.service.tick(self.config,self.github,self.settings,launch=False)
        self.assertFalse(self.service.active_jobs())

    def test_stale_proposal_action_is_not_applied_by_agent(self):
        job=self.start(); self.finish(job)
        self.assertEqual(self.runner.prepare.call_count,1)
        with patch('forge.factory.issue_actions.request',return_value='receipt') as request:
            self.assertEqual(self.service.apply('1:2',job['id'],actor_id='github:999'),'receipt')
            self.assertEqual(request.call_args.kwargs['revision'],'revision-a')
            self.assertEqual(request.call_args.kwargs['actor_id'],'github:999')
            self.assertEqual(request.call_args.kwargs['request_id'],job['id'])
            self.assertEqual(self.service.apply('1:2',job['id']),'receipt')
            self.assertEqual(request.call_count,1)

    def test_invalid_proposal_becomes_classified_exit(self):
        job=self.start(); self.finish(job,proposal={'action':'merge','reason':'skip checks'})
        self.assertFalse(any(m['role']=='assistant' for m in self.service.view('1:2')['messages']))
        self.assertEqual(self.service.view('1:2')['status'],'unavailable')
        self.service.drive(job,self.github)
        self.assertFalse(self.service.active_jobs())

    def test_worker_result_identity_must_match_assignment(self):
        job=self.start(); self.finish(job,attempt='someone-else')
        self.assertEqual(self.service.view('1:2')['status'],'unavailable')
        self.assertFalse(any(m['role']=='assistant' for m in self.service.view('1:2')['messages']))

    def test_rotating_credentials_remain_leased_until_verified_cleanup(self):
        self.config['provider_auth_file_env']='CODEX_ACCOUNT'
        job=self.start()
        with self.store.read_snapshot() as db:
            self.assertFalse(provider_available(db,self.config['provider'],None,self.config,self.now))
            self.assertTrue(provider_available(db,self.config['provider'],None,{**self.config,'provider_auth_file_env':'OTHER'},self.now))
        self.service.cancel('1:2'); self.service.drive(job,self.github)
        with self.store.read_snapshot() as db:
            self.assertTrue(provider_available(db,self.config['provider'],None,self.config,self.now))

    def test_auth_failure_retries_once_and_preserves_explanation(self):
        job=self.start()
        self.runner.inspect.return_value={'status':'exited','running':False}
        self.runner.capture.return_value={'result':None,'logs':json.dumps({'type':'error','error':{'message':'not logged in'}}),'execution':{'exit_code':1}}
        self.service.drive(job,self.github)
        thread=self.service.read()['1:2']
        self.assertEqual(thread['status'],'queued')
        self.assertTrue(thread['auto_retry'])
        self.assertEqual(thread['messages'][-1]['reason'],'auth')
        self.service.drive(job,self.github)
        self.assertFalse(self.service.active_jobs())
        self.service.tick(self.config,self.github,self.settings)
        self.assertEqual(self.service.view('1:2')['presence']['code'],'accounts')

    def test_raw_logs_expire_after_cleanup_but_answers_remain(self):
        job=self.start(); self.finish(job)
        self.now+=31*86400
        self.service.commands.prune_logs()
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM discussion_artifacts WHERE kind='logs'").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM discussion_artifacts WHERE kind='result'").fetchone()[0],1)
        self.assertEqual(len(self.service.view('1:2')['messages']),2)

    def test_queued_cancel_has_left_presence_and_no_worker(self):
        self.send(); self.service.cancel('1:2')
        view=self.service.view('1:2')
        self.assertEqual(view['presence']['phase'],'left')
        self.assertEqual(view['messages'][-1]['reason'],'cancelled')
        self.service.cancel('1:2')
        self.assertEqual(len(self.service.view('1:2')['messages']),2)

    def test_restart_recovers_running_worker_and_cleanup(self):
        job=self.start()
        resumed=Discussions(self.root,store=self.store,runner=self.factory,inventory=self.inventory,clock=lambda:self.now,boot='new-boot')
        self.runner.inspect.return_value={'status':'running','running':True}
        self.runner.observe.return_value={'logs':''}
        resumed.drive(job,self.github)
        self.assertEqual(resumed.active_jobs()[0]['boot'],'new-boot')
        self.runner.start.assert_called_once()
        resumed.cancel('1:2'); resumed.drive(job,self.github)
        self.assertFalse(resumed.active_jobs())
