"""Multiple real shell passes with stateful external boundaries, never host tmux/GitHub."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from forge.activity import read_status, record
from forge.handover_demand import read_demand
import sqlite3
import time
from forge.scheduling import issue_revision, repo_state
from forge.control import atomic_json

ROOT = Path(__file__).parents[1]


class LifecycleIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for directory in ('repos.d','bin','roles','mockbin','state'):
            (self.root/directory).mkdir()
        shutil.copy2(ROOT/'legacy/supervisor.sh',self.root/'supervisor.sh')
        shutil.copy2(ROOT/'bin/forge',self.root/'bin/forge')
        shutil.copytree(ROOT/'forge',self.root/'forge',ignore=shutil.ignore_patterns('__pycache__'))
        for role in ('build','review','audit'):
            (self.root/('fixture' if role=='build' else 'fixture-'+role)/'.git').mkdir(parents=True)
        (self.root/'repos.d/fixture.repo').write_text('REPO=o/fixture\nOWNER_LABEL=auto:owned-by-fixture\nRECOVERY_AUTHOR=fixture-bot\n')
        (self.root/'notify.sh').write_text('#!/bin/sh\nexit 0\n')
        (self.root/'notify.sh').chmod(0o755)
        for command in ('tmux','gh','git','ps','sleep','claude'):
            path=self.root/'mockbin'/command
            shutil.copy2(ROOT/'tests/fixtures/lifecycle_tools.py',path)
            path.chmod(0o755)
        # Closed PATH: no fallback to real tmux, gh, git or a provider, even after refactors.
        for command in ('bash','sh','python3','timeout','dirname','mkdir','flock','id','date','grep','sha256sum','cut','touch','env'):
            (self.root/'mockbin'/command).symlink_to(shutil.which(command))
        # Deterministic host telemetry only; every Forge module and subprocess stays real.
        (self.root/'sitecustomize.py').write_text("""from pathlib import Path
original = Path.read_text
def read(self,*args,**kwargs):
    if str(self)=='/proc/meminfo': return 'MemTotal: 8388608 kB\\nMemAvailable: 6291456 kB\\n'
    if str(self)=='/proc/pressure/memory': return 'some avg10=0 avg60=0 avg300=0 total=0\\nfull avg10=0 avg60=0 avg300=0 total=0\\n'
    return original(self,*args,**kwargs)
Path.read_text=read
""")
        self.env=dict(os.environ,PATH=str(self.root/'mockbin'),HOME=str(self.root),PYTHONPATH=str(self.root),
            FORGE_ONCE='1',FORGE_QUIET_START='1',FORGE_CODEX_BIN=str(self.root/'mockbin/missing-codex'),FIXTURE_PANE_PID=str(os.getpid()),FORGE_CLAUDE_BIN=str(self.root/'mockbin/claude'),FIXTURE_ROOT=str(self.root))
        atomic_json(repo_state(self.root/'state','o/fixture'), {'issues':{str(number):{'triaged_revision':issue_revision(dict(number=number,title=title,body='',labels=[{'name':'ready'},{'name':'p2'}]))} for number,title in ((1,'Fixture work'),(3,'Next work'))}})
        self.write_world(dict(phase='ready',sessions={},next_id=0,dispatches=[],released=[]))

    def world(self):
        return json.loads((self.root/'world.json').read_text())

    def write_world(self,world):
        (self.root/'world.json').write_text(json.dumps(world))

    def execute(self,args):
        result=subprocess.run(args,env=self.env,cwd=self.root,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)
        unexpected=self.root/'unexpected.jsonl'
        self.assertFalse(unexpected.exists(),unexpected.read_text() if unexpected.exists() else '')
        return result

    def supervisor(self):
        self.execute(['/bin/bash',str(self.root/'supervisor.sh')])

    def observe(self):
        self.execute(['/usr/bin/python3','-m','forge.activity','--state',str(self.root/'state'),
            '--config',str(self.root/'repos.d'),'sample'])
        return read_status(self.root/'state')['sessions']['fixture-build']

    def test_create_dispatch_busy_idle_verified_release_and_replacement(self):
        self.supervisor()
        first=self.world()['sessions']['fixture-build']['id']
        busy=self.observe()
        self.assertEqual(busy['state'],'busy')
        self.assertIsNotNone(busy['dispatched_at'])
        self.assertEqual(len(self.world()['dispatches']),1)
        # Provider published a canonical PR but still busy: no cleanup is allowed.
        world=self.world();world['phase']='published';self.write_world(world)
        self.supervisor()
        self.assertIn('fixture-build',self.world()['sessions'])
        self.assertNotIn(first,self.world()['released'])
        retained=json.loads((self.root/'state/lifecycle/fixture-build.json').read_text())
        self.assertEqual(retained['reason'],'not_explicitly_idle')
        self.assertEqual(retained['outcome'],'retained')
        # Idle with a dirty checkout is also retained, with an explicit proof refusal.
        world=self.world();world['sessions']['fixture-build']['state']='idle';world['dirty']=True;self.write_world(world)
        self.supervisor()
        self.assertIn('fixture-build',self.world()['sessions'])
        retained=json.loads((self.root/'state/lifecycle/fixture-build.json').read_text())
        self.assertEqual(retained['reason'],'worktree_not_clean')
        self.assertEqual(retained['outcome'],'retained')
        world=self.world();world['dirty']=False;self.write_world(world)
        idle=self.observe()
        self.assertEqual(idle['state'],'idle')
        self.assertEqual(idle['run_id'],busy['run_id'])
        self.supervisor()
        self.assertNotIn('fixture-build',self.world()['sessions'])
        self.assertIn(first,self.world()['released'])
        receipt=json.loads((self.root/'state/handoffs/fixture-build.json').read_text())
        self.assertEqual(receipt['reason'],'verified_handoff')
        self.assertEqual(receipt['evidence']['issue'],1)
        self.assertEqual(receipt['evidence']['pr'],2)
        self.assertFalse((self.root/'state/fixture-build.up').exists())
        released=json.loads((self.root/'state/lifecycle/fixture-build.json').read_text())
        self.assertEqual(released['outcome'],'released')
        self.assertEqual(released['generation']['id'],busy['generation_id'])
        world=self.world();world['phase']='replacement';self.write_world(world)
        self.supervisor()
        self.assertNotEqual(self.world()['sessions']['fixture-build']['id'],first)
        replacement=self.observe()
        self.assertEqual(replacement['state'],'busy')
        self.assertIsNotNone(replacement['dispatched_at'])
        self.assertNotEqual(replacement['run_id'],busy['run_id'])
        events=read_status(self.root/'state')['events']
        self.assertTrue(any(e['event']=='session_stopped' and e['metadata']['reason']=='verified_handoff' for e in events))
        self.assertEqual(len([e for e in events if e['event']=='dispatch' and e['session']=='fixture-build']),2)

    def test_codex_real_json_runner_receipt_and_unchanged_work_not_resubmitted(self):
        binary=self.root/'mockbin/codex'
        shutil.copy2(ROOT/'tests/fixtures/lifecycle_tools.py',binary);binary.chmod(0o755)
        self.env['FORGE_CODEX_BIN']=str(binary)
        atomic_json(self.root/'state/routing.json',{'last_provider':'claude'})
        self.supervisor()
        receipt=json.loads((self.root/'state/codex-workers/fixture-build.json').read_text())
        self.assertEqual(receipt['provider'],'codex')
        self.assertEqual(receipt['state'],'completed')
        self.assertTrue(receipt['turn_completed'])
        observed=self.observe()
        self.assertEqual(observed['provider'],'codex')
        self.assertEqual(observed['state'],'idle')
        self.assertIsNotNone(observed['execution_started_at'])
        before=len(self.world()['dispatches'])
        self.supervisor()
        self.assertEqual(len(self.world()['dispatches']),before)
        self.assertIn('fixture-build',self.world()['sessions'])
        calls=[json.loads(line) for line in (self.root/'calls.jsonl').read_text().splitlines()]
        self.assertEqual(len([call for call in calls if call[:2]==['codex','exec']]),1)

    def test_verified_handover_restarts_only_preserved_zero_queue_claim(self):
        world=self.world();world['phase']='claimed';world['next_id']=1;self.write_world(world)
        record(self.root/'state','session_created','fixture-build',now=time.time()-10)
        record(self.root/'state','session_stopped','fixture-build',now=time.time()-5)
        with sqlite3.connect(self.root/'state/activity.sqlite3') as db:
            creation=db.execute("SELECT id FROM events WHERE event='session_created'").fetchone()[0]
        manifest=dict(session='fixture-build',repo='o/fixture',role='build',released=True,
                      released_at=time.time()-5,creation_event_id=creation,claims=[1])
        atomic_json(self.root/'state/provider-handovers/fixture-build.json',manifest)
        self.supervisor()
        snapshot=json.loads(next((self.root/'state/snapshots').glob('*.json')).read_text())
        self.assertEqual(snapshot['counts']['builder'],0)
        self.assertEqual(snapshot['schedule']['active'],[1])
        self.assertIn('fixture-build',self.world()['sessions'])
        self.assertEqual(self.world()['sessions']['fixture-build']['id'],'$2')
        self.assertEqual(self.observe()['state'],'busy')
        calls=[json.loads(line) for line in (self.root/'calls.jsonl').read_text().splitlines()]
        tasks=[call[-1] for call in calls if call[:2]==['tmux','send-keys'] and '-l' in call and 'Read your role file' in call[-1]]
        self.assertEqual(len(tasks),1)
        self.assertIn(str(self.root/'state/provider-handovers/fixture-build.json'),tasks[0])
        self.assertIn('before any new claim',tasks[0])
        self.assertEqual(read_demand(self.root/'repos.d',self.root/'state','fixture-build')['demand'],0)
