import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forge.store import db as storage, upgrade
from forge.store.configuration import Configuration


class StewardMigrationTests(unittest.TestCase):
    def test_upgrade_preserves_history_and_explicit_role_allocations(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch('forge.store.db._migrations',return_value=storage._migrations()[:2]):
                old=storage.Store.create(Path(temporary)/'store'); config=Configuration(old)
                config.replace('runtime.json',{'slots':{'recovery':2},'timeout_seconds':{'recovery':450}},expected_revision=0,credential_ref='private-ref')
                config.replace('setup/runtime.json',{'enabled':False},expected_revision=0)
                config.replace('host-runtime.json',{'slots':{'steward':3},'timeout_seconds':{'steward':700}},expected_revision=0)
            target=storage.Store(old.root);upgrade.apply(target);config=Configuration(target)
            self.assertEqual(config.get('runtime.json')['settings'],{'slots':{'recovery':2,'steward':2},'timeout_seconds':{'recovery':450,'steward':450}})
            self.assertEqual(config.get('setup/runtime.json')['settings'],{'enabled':False,'slots':{'steward':1},'timeout_seconds':{'steward':600}})
            self.assertEqual(config.get('host-runtime.json')['revision'],1)
            self.assertEqual(config.get('runtime.json')['credential_ref'],'private-ref')
            with target.read_snapshot() as db:
                historical=db.execute("SELECT settings_json,active FROM configuration WHERE domain='runtime.json' AND revision=1").fetchone()
                self.assertEqual(json.loads(historical[0]),{'slots':{'recovery':2},'timeout_seconds':{'recovery':450}})
                self.assertEqual(historical[1],0)
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            upgrade.apply(target)
            self.assertEqual(Configuration(target).get('runtime.json')['revision'],2)
