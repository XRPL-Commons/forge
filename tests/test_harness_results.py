import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge.factory.config import validate
from forge.factory.runner import ContainerRunner
from forge.harnesses import results
from forge.harnesses.registry import adapter


class ResultBoundaryTests(unittest.TestCase):
    def test_every_adapter_hands_off_role_limit_and_rejects_one_extra_byte(self):
        for name in ('claude', 'grok', 'codex'):
            module = adapter(name)
            for role, limit in (('build', 65536), ('triage', 4*1048576)):
                for excess in (0, 1):
                    with self.subTest(provider=name, role=role, excess=excess), tempfile.TemporaryDirectory() as temporary:
                        root = Path(temporary)
                        result = {'summary': 'x'*(limit-len(json.dumps({'summary': ''}).encode())+excess)}
                        note = {'role': role, 'id': 'job'}
                        completed = subprocess.CompletedProcess([], 0)
                        namespace = module['run'].__globals__
                        if name == 'codex':
                            (root/'final-result.json').write_text(json.dumps(result))
                        replacements = {
                            'Path': lambda path: root/Path(path).name,
                            'prepare_tool_environment': lambda *args: {},
                            'invoke': lambda *args: (result, None),
                        }
                        with patch.dict(namespace, replacements), patch('builtins.print'), patch('subprocess.run', return_value=completed), \
                             patch('runpy.run_path', return_value={}), \
                             patch.dict(module['EXECUTION'], {'stream': lambda *args: (0, [])}), \
                             patch.dict(module['RESULTS'], {'write_result': lambda n,r: results.write_result(n,r,root)}):
                            if excess:
                                with self.assertRaisesRegex(ValueError, 'size limit'):
                                    module['run'](note,root,{}, {})
                                self.assertFalse((root/'result.json').exists())
                            else:
                                self.assertEqual(module['run'](note,root,{}, {}),0)
                                self.assertEqual(json.loads((root/'result.json').read_text()),result)
                                self.assertFalse((root/'final-result.json').exists())

    def test_host_capture_obeys_same_boundary_and_scrubs_worker_logs(self):
        for role, limit in (('build',65536),('triage',4*1048576)):
            for excess in (0,1):
                with self.subTest(role=role,excess=excess):
                    raw=json.dumps({'summary':'x'*(limit-len(json.dumps({'summary':''}).encode())+excess)}).encode()
                    archive=io.BytesIO()
                    with tarfile.open(fileobj=archive,mode='w') as tar:
                        info=tarfile.TarInfo('result.json');info.size=len(raw)
                        tar.addfile(info,io.BytesIO(raw))
                    runner=ContainerRunner(validate({}))
                    runner.inspect=Mock(return_value={'id':'container','running':False})
                    runner._scrub_values=Mock(return_value=['synthetic-secret'])
                    runner.call=Mock(side_effect=[Mock(stdout=archive.getvalue()),Mock(stdout=b'error synthetic-secret')])
                    capture=runner.capture({'id':'job','role':role})
                    self.assertNotIn('synthetic-secret',capture['logs'])
                    if excess:
                        self.assertIsNone(capture['result'])
                        self.assertIn('valid result artifact',capture['missing'])
                    else:
                        self.assertEqual(capture['result']['summary'],'x'*(limit-len(json.dumps({'summary':''}).encode())))

    def test_invalid_provider_file_is_never_published(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'final-result.json'
            path.write_text('{"partial":')
            with self.assertRaises(ValueError):
                results.publish_result({'role':'triage'},path)
            self.assertFalse((path.parent/'result.json').exists())
            self.assertTrue(path.is_file())
