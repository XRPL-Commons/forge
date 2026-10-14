"""Selected storage must initialize adapters through the real module launchers."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from forge.onboarding import Onboarding
from forge.settings import Settings, DEFAULT
from tests.test_sqlite_identity import selected_store


class ModuleEntrypointTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()
        self.store=selected_store(self.root)
        self.source=Path(__file__).resolve().parents[1]

    def test_real_service_module_and_launch_complete_selected_tick(self):
        setup=Onboarding(self.root)
        setup.read()
        with setup.store.transaction() as data:
            data['phase']='active'
        for modules in (['forge.setup_service'],['forge.launch','forge.setup_service']):
            with self.subTest(modules=modules):
                with self.store.write() as db:
                    db.execute("DELETE FROM store_metadata WHERE key='supervisor:heartbeat'")
                    db.execute("DELETE FROM runtime_controls WHERE name='setup_service'")
                process=subprocess.Popen([sys.executable,'-m',*modules,'--root',str(self.root)],
                    cwd=self.source,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                try:
                    deadline=time.monotonic()+10
                    completed=False
                    while process.poll() is None and time.monotonic()<deadline:
                        with self.store.read_snapshot() as db:
                            completed=bool(db.execute("SELECT 1 FROM store_metadata WHERE key='supervisor:heartbeat'").fetchone())
                        if completed:break
                        time.sleep(.02)
                    if process.poll() is None:process.terminate()
                    stdout,stderr=process.communicate(timeout=10)
                    self.assertEqual(process.returncode,0,stderr or stdout)
                    self.assertTrue(completed,'Selected supervisor never completed its SQL cycle: '+stdout+stderr)
                    self.assertNotIn('Traceback',stderr)
                    self.assertNotIn('Supervisor enquiry failed',stdout)
                    with self.store.read_snapshot() as db:
                        self.assertIsNotNone(db.execute("SELECT heartbeat_at FROM runtime_controls WHERE name='setup_service'").fetchone())
                finally:
                    if process.poll() is None:process.kill();process.communicate()
        self.assertFalse((self.root/'state/setup-service.json').exists())

    def test_real_settings_modules_read_and_validate_sql_configuration(self):
        Settings(self.root).save_config(DEFAULT)
        self.assertFalse((self.root/'factory.json').exists())
        for modules in (['forge.settings'],['forge.launch','forge.settings']):
            with self.subTest(modules=modules):
                result=subprocess.run([sys.executable,'-m',*modules,'show','--root',str(self.root)],
                    cwd=self.source,capture_output=True,text=True,timeout=10)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(json.loads(result.stdout)['config'],DEFAULT)
                result=subprocess.run([sys.executable,'-m',*modules,'validate','--root',str(self.root)],
                    cwd=self.source,capture_output=True,text=True,timeout=10)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertIn('Configuration is valid',result.stdout)
                result=subprocess.run([sys.executable,'-m',*modules,'sync','--root',str(self.root)],
                    cwd=self.source,capture_output=True,text=True,timeout=10)
                # Empty App connections need configuration, but sync still reads
                # the SQL document and returns its public view without network.
                self.assertEqual(result.returncode,1,result.stderr)
                self.assertEqual(json.loads(result.stdout)['config'],DEFAULT)

