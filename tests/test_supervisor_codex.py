"""Supervisor-side Codex preparation never starts a real provider."""
import json
import shlex
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from forge.startup import codex_command
from tests.host_isolation import deny_host_tmux


class CodexPreparationTests(unittest.TestCase):
    def setUp(self):
        deny_host_tmux(self)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.state=self.root/'state';self.config=self.root/'repos.d';self.config.mkdir()
        self.clone=self.root/'clone with spaces';self.clone.mkdir()
        (self.config/'one.repo').write_text(f'REPO=o/one\nOWNER_LABEL=auto:owned-by-test\nCLONE_BUILD="{self.clone}"\n')
        self.binary=self.root/'codex executable';self.binary.write_text('#!/bin/sh\nexit 0\n');self.binary.chmod(0o700)
        self.generation='$1|100|%1|10';self.progress='work-one'
        self.identity=self.generation+'|bash|'+str(self.clone)
        self.pane='user@host:~/one$';self.processes='10 1 bash';self.remote='git@github.com:o/one.git'
        self.identities=0;self.change=None;self.calls=[]
        self.observation={'state':'idle','thread_id':'11111111-1111-4111-8111-111111111111'}

    def fake_run(self,args,**kwargs):
        self.calls.append(args)
        if args[0]=='git':text=str(self.clone) if '--show-toplevel' in args else self.remote
        elif args[:2]==['tmux','list-panes']:
            self.identities+=1
            if self.identities==2 and self.change:self.change()
            text=self.identity
        elif args[:2]==['tmux','capture-pane']:text=self.pane
        elif args[0]=='ps':text=self.processes
        else:self.fail(args)
        return SimpleNamespace(returncode=0,stdout=text,stderr='')

    def prepare(self,resume=False):
        with patch('forge.lifecycle.progress_key',side_effect=lambda *a,**k:self.progress),patch('forge.codex_worker.observe',return_value=self.observation):
            return codex_command(self.config,self.state,'one-build',str(self.binary),self.generation,'Read role; preserve local work.',resume=resume,run=self.fake_run)

    def test_private_prompt_and_exact_quoted_runner_command(self):
        args=shlex.split(self.prepare())
        self.assertIn('forge.codex_worker',args)
        self.assertEqual(args[args.index('--generation')+1],self.generation)
        self.assertEqual(args[args.index('--executable')+1],str(self.binary))
        prompt=Path(args[args.index('--prompt-file')+1])
        self.assertEqual(prompt.stat().st_mode&0o777,0o600)
        self.assertIn('preserve local work',prompt.read_text())
        self.assertNotIn('--resume-thread',args)
        self.assertFalse(any(c[0]==str(self.binary) for c in self.calls))

    def test_resume_requires_changed_work_and_exact_completed_thread(self):
        self.prepare()
        with self.assertRaisesRegex(ValueError,'changed_work'):self.prepare(resume=True)
        self.progress='work-two'
        args=shlex.split(self.prepare(resume=True))
        self.assertEqual(args[args.index('--resume-thread')+1],self.observation['thread_id'])
        self.progress='work-three';self.observation['state']='busy'
        with self.assertRaisesRegex(ValueError,'completed_execution'):self.prepare(resume=True)

    def test_typed_input_child_work_wrong_remote_unknown_work_are_preserved(self):
        for key,value in [('pane','user@host:~/one$ unfinished'),('processes','10 1 bash\n11 10 test'),('remote','git@github.com:other/repo.git'),('progress',None)]:
            with self.subTest(key=key):
                old=getattr(self,key);setattr(self,key,value)
                with self.assertRaises(ValueError):self.prepare()
                setattr(self,key,old)

    def test_generation_or_fresh_work_change_aborts_command(self):
        self.change=lambda:setattr(self,'identity',self.identity.replace('$1|','$2|'))
        with self.assertRaisesRegex(ValueError,'evidence_changed'):self.prepare()
        self.assertFalse((self.state/'codex-dispatches/one-build.json').exists())


    def test_child_or_input_appearing_during_preparation_prevents_dispatch(self):
        self.change=lambda:setattr(self,'processes','10 1 bash\n11 10 test')
        with self.assertRaisesRegex(ValueError,'shell_changed'):self.prepare()
        self.processes='10 1 bash';self.identities=0
        self.change=lambda:setattr(self,'pane','user@host:~/one$ unfinished')
        with self.assertRaisesRegex(ValueError,'shell_changed'):self.prepare()

    def test_cli_reports_fixed_preparation_reason_without_raw_errors(self):
        import contextlib
        import io
        from forge.startup import main
        args=['startup','codex-command','--config',str(self.config),'--state',str(self.state),
              '--session','one-build','--generation',self.generation,'--resume']
        for error,expected in [(ValueError('resume_requires_changed_work'),'resume_requires_changed_work'),
                               (ValueError('PRIVATE_PATH_OR_PROVIDER_TEXT'),'codex_preparation_unavailable')]:
            output=io.StringIO()
            with patch('sys.argv',args), patch('forge.startup.codex_command',side_effect=error), contextlib.redirect_stdout(output):
                self.assertEqual(main(),1)
            self.assertEqual(json.loads(output.getvalue()),{'reason':expected})
            self.assertNotIn('PRIVATE',output.getvalue())


class CodexStartupRetryTests(unittest.TestCase):
    fake_run = CodexPreparationTests.fake_run
    prepare = CodexPreparationTests.prepare

    def setUp(self):
        CodexPreparationTests.setUp(self)
        from forge.codex_worker import parse_generation
        command = shlex.split(self.prepare())
        self.original_prompt = Path(command[command.index('--prompt-file')+1])
        self.identities = 0
        self.worker_root = self.state/'codex-workers'; self.worker_root.mkdir(mode=0o700)
        (self.worker_root/'one-build.lock').touch(mode=0o600)
        self.failed_id = '22222222-2222-4222-8222-222222222222'
        logs = self.worker_root/'logs'/'one-build'/self.failed_id; logs.mkdir(parents=True)
        (logs/'events.jsonl').write_text('')
        (logs/'stderr.log').write_text("error: the argument '--sandbox <SANDBOX_MODE>' cannot be used with '--approve-for-me'\n")
        self.receipt = {'provider':'codex','session':'one-build','repo':'o/one','role':'build',
            'clone':str(self.clone),'generation':parse_generation(self.generation), 'run_id':self.failed_id,
            'state':'failed','exit_code':2,'event_count':0,'thread_id':None,'remaining_processes':[],
            'raw_log':str(logs/'events.jsonl')}
        self.receipt_path = self.worker_root/'one-build.json'; self.save()

    def save(self):
        self.receipt_path.write_text(json.dumps(self.receipt))

    def retry(self):
        from forge.codex_worker import prepare_retry
        with patch('forge.lifecycle.progress_key',side_effect=lambda *a,**k:self.progress):
            return prepare_retry(self.config,self.state,'one-build',str(self.binary),self.generation,run=self.fake_run)

    def test_retry_is_once_and_preserves_exact_original_prompt(self):
        args=shlex.split(self.retry())
        self.assertEqual(args[args.index('--prompt-file')+1],str(self.original_prompt))
        self.assertNotIn('--resume-thread',args)
        self.assertEqual(self.identities,2)
        with self.assertRaisesRegex(ValueError,'already_prepared'):self.retry()
        self.assertEqual(json.loads(self.receipt_path.read_text()),self.receipt)

    def test_retry_rejects_task_started_wrong_generation_and_other_failure(self):
        for changes in ({'thread_id':'33333333-3333-4333-8333-333333333333'}, {'event_count':1}, {'exit_code':1}, {'generation':{}}):
            original=self.receipt.copy();self.receipt.update(changes);self.save()
            with self.assertRaisesRegex(ValueError,'not_verified'):self.retry()
            self.receipt=original;self.save()

    def test_retry_rejects_children_typed_input_and_changed_generation(self):
        for attr,value in [('processes','10 1 bash\n11 10 test'),('pane','user@host:~/one$ text'),('identity',self.identity.replace('$1|','$2|'))]:
            original=getattr(self,attr);setattr(self,attr,value)
            with self.assertRaises(ValueError):self.retry()
            setattr(self,attr,original)
        self.assertFalse((self.worker_root/('retry-'+self.failed_id+'.json')).exists())

    def test_retry_rejects_new_work_during_second_proof(self):
        self.change=lambda:setattr(self,'processes','10 1 bash\n11 10 test')
        with self.assertRaisesRegex(ValueError,'shell_changed'):self.retry()
        self.assertFalse((self.worker_root/('retry-'+self.failed_id+'.json')).exists())

    def test_retry_rejects_receipt_or_prompt_change_during_proof(self):
        self.change=lambda:self.receipt_path.write_text('{}')
        with self.assertRaisesRegex(ValueError,'evidence_changed'):self.retry()
        self.assertFalse((self.worker_root/('retry-'+self.failed_id+'.json')).exists())
