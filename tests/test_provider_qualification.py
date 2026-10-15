from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from forge.provider_qualification import WORKER, qualify
from forge.factory.worker_boundary import BoundaryError
from forge_client.boundary_qualification import execute
from tests.test_worker_boundary import Engine


class ProviderQualificationTests(unittest.TestCase):
    def test_requires_native_linux_before_any_mutation(self):
        engine = Engine()
        with patch('forge.provider_qualification.platform.system', return_value='Darwin'):
            with self.assertRaisesRegex(BoundaryError, 'Linux'):
                qualify('sha256:'+'a'*64, docker=engine)
        self.assertEqual(engine.calls, [])

    def test_requires_supported_engine_before_mutation(self):
        engine = Engine(); engine.version = '27.0.0'
        with patch('forge.provider_qualification.platform.system', return_value='Linux'):
            with self.assertRaisesRegex(BoundaryError, 'Engine 28'):
                qualify('sha256:'+'a'*64, docker=engine)
        self.assertEqual(len(engine.calls), 1)
        self.assertFalse(any(engine.resources.values()))

    def test_supported_cli_selects_provider_canary_explicitly(self):
        args = SimpleNamespace(image='sha256:'+'a'*64, ssh='anvil', apply=True, provider_proxy=True)
        installation = Mock(); installation.journal.read.return_value = {}
        installation.host.rpc.return_value = {'result':dict(status='qualified', cleanup='confirmed', image=args.image)}
        execute(args, installation=installation)
        self.assertTrue(installation.host.rpc.call_args.args[1]['provider_proxy'])
        args.apply = False
        report = execute(args)
        self.assertIn('shell credential isolation', report['checks'])

    def test_worker_program_compiles(self):
        compile(WORKER, '<provider-qualification-worker>', 'exec')


if __name__ == '__main__': unittest.main()
