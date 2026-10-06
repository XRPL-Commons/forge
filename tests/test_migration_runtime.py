import io
import json
from pathlib import Path
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from forge.store.migration import runtime
from forge.store.migration.snapshot import MigrationError,digest


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        archive=io.BytesIO()
        with tarfile.open(fileobj=archive,mode='w:gz') as tar:
            payload=b'fixture configure script'
            member=tarfile.TarInfo(runtime.DIRECTORY+'/configure');member.size=len(payload)
            tar.addfile(member,io.BytesIO(payload))
        self.source=archive.getvalue()
        self.calls=[]
        self.install=None
        def run(args,**kwargs):
            self.calls.append(args)
            if args[0].endswith('/configure'):
                self.install=Path(next(v.removeprefix('--prefix=') for v in args if v.startswith('--prefix=')))
            if args==['make','install']:
                (self.install/'lib').mkdir(parents=True)
                (self.install/'lib/libsqlite3.so.0.8.6').write_bytes(b'qualified library fixture')
            return SimpleNamespace(returncode=0,stdout=json.dumps(dict(version='3.51.3',journal_mode='wal',synchronous=2,foreign_keys=1,integrity='ok')))
        self.run=run
        for name,value in [('sys.platform','linux'),('SOURCE_SHA',digest(self.source))]:
            p=patch('forge.store.migration.runtime.'+name,value);p.start();self.addCleanup(p.stop)
        p=patch('forge.store.migration.runtime.shutil.which',return_value='/usr/bin/tool');p.start();self.addCleanup(p.stop)

    def test_build_is_qualified_before_publication_and_repeat_verifies_same_library(self):
        result=runtime.prepare(self.root,run=self.run,download=lambda *a,**k:io.BytesIO(self.source))
        self.assertTrue(result['changed']);self.assertFalse(result['activation'])
        self.assertEqual(result['qualification']['synchronous'],2)
        self.assertFalse(any('systemctl' in call or 'apt-get' in call for call in self.calls))
        def no_download(*args,**kwargs):raise AssertionError('existing runtime should not download again')
        again=runtime.prepare(self.root,run=self.run,download=no_download)
        self.assertFalse(again['changed'])
        library=Path(result['library_directory'])/'libsqlite3.so.0.8.6'
        library.write_bytes(b'corrupt library')
        with self.assertRaisesRegex(MigrationError,'sqlite_runtime_identity_mismatch'):
            runtime.prepare(self.root,run=self.run,download=no_download)

    def test_wrong_source_digest_never_reaches_a_build_command(self):
        with self.assertRaisesRegex(MigrationError,'sqlite_source_digest_mismatch'):
            runtime.prepare(self.root,run=self.run,download=lambda *a,**k:io.BytesIO(b'wrong source'))
        self.assertEqual(self.calls,[])

    def test_failed_qualification_never_publishes_a_runtime(self):
        def run(args,**kwargs):
            if '-I' in args:return SimpleNamespace(returncode=0,stdout='{"version":"3.45.1"}')
            return self.run(args,**kwargs)
        with self.assertRaisesRegex(MigrationError,'sqlite_runtime_qualification_failed'):
            runtime.prepare(self.root,run=run,download=lambda *a,**k:io.BytesIO(self.source))
        self.assertEqual(list((self.root/'runtimes').glob('sqlite-*')),[])


if __name__=='__main__':unittest.main()
