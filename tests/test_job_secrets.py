import json
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge.credentials import Credentials
from forge.factory.config import validate
from forge.factory.runner import ContainerRunner, scrub

REF = 'FORGE_SETUP_CLAUDE_AUTH'
LOGIN = {'claudeAiOauth': {'accessToken': 'synthetic-access-value-0123', 'refreshToken': 'synthetic-refresh-value-4567',
                           'expiresAt': 1, 'scopes': ['user:inference']}}
TOKEN = 'ghs_' + 'S' * 36
JOB = {'id': '1c3a6c52-3d0e-4d6b-9d65-6f1c2b8e4a10', 'fenced': False, 'deadline': 5000}


def archive(text):
    content = io.BytesIO()
    with tarfile.open(fileobj=content, mode='w') as tar:
        raw = text.encode(); member = tarfile.TarInfo('result.json'); member.size = len(raw)
        tar.addfile(member, io.BytesIO(raw))
    return content.getvalue()


class JobSecretTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        Credentials(self.root).put(REF, json.dumps(LOGIN), file=True)
        config = validate({'provider': 'claude', 'provider_auth_file_env': REF, 'provider_environment': []})
        self.revoke = Mock(return_value='revoked')
        self.runner = ContainerRunner(config, environ=Credentials(self.root).environment(), revoke=self.revoke)
        self.runner.call = Mock()
        self.runner.inspect = Mock(return_value={'status': 'created', 'id': 'worker', 'running': False})
        self.runner.source_preparation = Mock()
        self.runner.source_preparation.mirror.prepared.return_value = {
            'source_input': {}, 'publication_authorization': {}, 'execution_fence': 'fence',
            'session_id': 'session', 'revoked_at': None}
        self.runner.source_preparation.mirror.live_authority.return_value = True
        self.runner.boundary = Mock(return_value=Mock())
        self.runner.prepare_source = Mock()
        self.runner.prepare(dict(JOB))
        self.path = self.runner._secrets_path(JOB)

    def test_prepare_keeps_the_job_secrets_private(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        stored = json.loads(self.path.read_text())
        self.assertNotIn('github_token', stored)
        with tarfile.open(fileobj=io.BytesIO(self.runner.call.call_args.kwargs['input'])) as payload:
            self.assertEqual(payload.getnames(), ['credentials.json'])
            delivered = json.load(payload.extractfile('credentials.json'))
            self.assertNotIn('GH_TOKEN', delivered)
            self.assertNotIn('GITHUB_TOKEN', delivered)
        self.assertIn('synthetic-refresh-value-4567', stored['values'])

    def test_worker_output_never_carries_the_login_or_github_token(self):
        leak = f'token {TOKEN} refresh synthetic-refresh-value-4567 access synthetic-access-value-0123'
        def call(argv, **kwargs):
            if argv[0] == 'cp':
                return Mock(stdout=archive(json.dumps({'summary': leak})))
            return Mock(stdout=leak.encode())
        self.runner.call = Mock(side_effect=call)
        captured, observed = self.runner.capture(JOB), self.runner.observe(JOB)
        for text in (json.dumps(captured['result']), captured['logs'], observed['logs']):
            for secret in (TOKEN, 'synthetic-refresh-value-4567', 'synthetic-access-value-0123'):
                self.assertNotIn(secret, text)

    def test_reset_revokes_legacy_token_and_forgets_it(self):
        self.path.write_text(json.dumps({"github_token": TOKEN}))
        self.runner.inspect = Mock(side_effect=[None, None])
        receipt = self.runner.reset({**JOB, 'fenced': True})
        self.revoke.assert_called_once_with(TOKEN)
        self.assertEqual(receipt['token_revocation'], 'revoked')
        self.assertFalse(self.path.exists())

    def test_a_failed_revocation_does_not_block_teardown(self):
        self.path.write_text(json.dumps({'github_token': TOKEN}))
        self.revoke.return_value = 'failed'
        self.runner.inspect = Mock(side_effect=[None, None])
        receipt = self.runner.reset({**JOB, 'fenced': True})
        self.assertTrue(receipt['capacity_ready'])
        self.assertEqual(receipt['token_revocation'], 'failed')
        self.assertFalse(self.path.exists())

    def test_revoked_assignment_does_not_receive_credentials(self):
        self.runner.call.reset_mock()
        self.runner.source_preparation.mirror.live_authority.return_value = False
        from forge.factory.runner import RunnerError
        with self.assertRaises(RunnerError):
            self.runner.prepare(dict(JOB))
        self.runner.call.assert_not_called()

    def test_jwt_shaped_tokens_are_scrubbed_without_being_known(self):
        # Assembled at runtime so secret scanners do not flag a literal token in source.
        header, body, signature = ('eyJ' + 'synthetic' + 'Header'), ('eyJ' + 'synthetic' + 'Claims'), 'synthetic' + 'Signature'
        self.assertNotIn(header, scrub('Bearer ' + '.'.join((header, body, signature))))


if __name__ == '__main__':
    unittest.main()
