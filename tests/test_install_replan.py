"""Replace only an unstarted installation, and present the human preflight once."""
from contextlib import nullcontext, redirect_stdout, redirect_stderr
from copy import deepcopy
from io import StringIO
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from forge_client.cli import main, parser, execute as cli_execute
from forge_client.contracts import SetupError, digest
from forge_client.install import Installation
from forge_client.installation import Install, execute
from forge_client.workflow import Journal
from tests import test_install_coordinator as coordinator
from tests.test_install_coordinator import interview, REVISION
from tests.test_fresh_install import response, STEPS


class ReplanTests(unittest.TestCase):
    def setUp(self):
        coordinator.CoordinatorTests.setUp(self)
        self.remote = Mock(); self.remote.call.return_value = response()
        self.lower = Journal('setup', 'anvil', root=self.journal.root)
        self.install.installation = Installation('anvil', host=self.remote, storage=self.lower, clock=lambda: 200)
        self.install.installation.execute('plan', name='Anvil', commit=REVISION)
        self.original = deepcopy(self.journal.read())
        self.old_lower = deepcopy(self.lower.read())
        self.changed = interview(); self.changed['revision'] = 'b'*40
        self.changed['repositories'] = self.changed['repositories'][:1]

    def test_replace_rebinds_both_journals_and_keeps_private_archives(self):
        self.install.preflight()
        original = deepcopy(self.journal.read())
        result = self.install.plan(self.changed, replace=True)
        self.assertEqual(result['intent']['revision'], 'b'*40)
        self.assertEqual(self.lower.read()['intent']['revision'], 'b'*40)
        self.assertIsNone(self.journal.read()['preflight'])
        self.assertEqual(self.journal.read()['stages'], {})
        archive = Journal('archived-installation', digest({'intent': original['inputs_digest'],
                          'started_at': original['started_at']}), root=self.journal.root)
        self.assertEqual(archive.read(), original)
        self.assertEqual(archive.path.stat().st_mode & 0o777, 0o600)
        archived_host = Journal('archived-setup', self.old_lower['intent']['id'], root=self.journal.root)
        self.assertEqual(archived_host.read(), self.old_lower)
        self.assertTrue(all(call.args[0] == 'plan' for call in self.remote.call.call_args_list))

    def test_any_stage_or_write_evidence_refuses_before_host_contact(self):
        cases = [{'stages': {'targets': {'status': 'pending'}}},
                 {'stages': {'host': {'status': 'applying', 'uncertain_write': True}}},
                 {'smoke_targets': {'publication': {'write_started': True}}},
                 {'smoke': {}}, {'consent': {}}]
        for update in cases:
            with self.subTest(update=update):
                self.install.state = {**deepcopy(self.original), **update}
                self.remote.call.reset_mock()
                with self.assertRaises(SetupError) as error:
                    self.install.plan(self.changed, replace=True)
                self.assertEqual(error.exception.code, 'installation_started')
                self.remote.call.assert_not_called()
                self.assertEqual(self.journal.read(), self.original)

    def test_remote_installation_and_identity_errors_preserve_both_journals(self):
        for status in ('installed', 'installing', 'interrupted'):
            with self.subTest(status=status):
                self.remote.call.return_value = response(self.old_lower['intent'], status,
                    'complete' if status == 'installed' else 'source', STEPS if status == 'installed' else STEPS[:2])
                with self.assertRaises(SetupError): self.install.plan(self.changed, replace=True)
                self.assertEqual(self.journal.read(), self.original)
                self.assertEqual(self.lower.read(), self.old_lower)
        self.remote.call.side_effect = SetupError('host_key_changed', 'Host identity changed')
        with self.assertRaises(SetupError): self.install.plan(self.changed, replace=True)
        self.assertEqual(self.journal.read(), self.original)
        self.assertEqual(self.lower.read(), self.old_lower)

    def test_invalid_replacement_does_not_touch_host_or_saved_plan(self):
        self.changed['repositories'][0]['merge_policy'] = 'invalid'
        self.remote.call.reset_mock()
        with self.assertRaises(SetupError): self.install.plan(self.changed, replace=True)
        self.remote.call.assert_not_called()
        self.assertEqual(self.journal.read(), self.original)
        self.assertEqual(self.lower.read(), self.old_lower)

    def test_retry_after_lower_journal_replacement_converges(self):
        with patch.object(self.journal, 'save', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError): self.install.plan(self.changed, replace=True)
        self.assertEqual(self.journal.read(), self.original)
        self.assertEqual(self.lower.read()['intent']['revision'], 'b'*40)
        resumed = Install('anvil', github=self.github, installation=self.install.installation,
                          journal=self.journal, preflight=self.observe)
        resumed.plan(self.changed, replace=True)
        self.assertEqual(self.journal.read()['intent']['revision'], 'b'*40)
        self.assertEqual(self.lower.read()['intent']['revision'], 'b'*40)

    def test_explicit_replace_resolves_null_revision_again_only_when_requested(self):
        requested = interview(); requested['revision'] = None
        self.install.state['request_digest'] = digest(requested); self.install.save()
        with patch('forge_client.installation.revision', return_value='c'*40) as resolve:
            self.install.plan(requested)
            resolve.assert_not_called()
            self.install.plan(requested, replace=True)
        self.assertEqual(self.journal.read()['intent']['revision'], 'c'*40)


class PresentationTests(unittest.TestCase):
    def run_wizard(self, *, blocked=False, flags=()):
        workflow = Mock(key='fixture', journal=Mock(root=Path('/unused')))
        workflow.preflight.return_value = {'status': 'preflight', 'host': {'ssh': 'anvil'},
            'items': [{'id': 'host.capacity', 'status': 'needs_you' if blocked else 'pass',
                       'detail': 'Capacity observed.', 'command': None}]}
        out, err = StringIO(), StringIO()
        with patch('forge_client.installation.Install', return_value=workflow), \
             patch('forge_client.installation.GitHub'), \
             patch('forge_client.installation.interview', return_value=interview()) as questions, \
             patch('forge_client.installation.connection_lock', return_value=nullcontext()), \
             patch('sys.stdin.isatty', return_value=True), patch('builtins.input', return_value='n') as apply, \
             redirect_stdout(out), redirect_stderr(err):
            code = main(['install', '--ssh', 'anvil', *flags])
        return workflow, questions, apply, code, out.getvalue() + err.getvalue()

    def test_blocked_and_declined_preflight_print_once(self):
        for blocked in (True, False):
            with self.subTest(blocked=blocked):
                workflow, _, apply, code, output = self.run_wizard(blocked=blocked)
                self.assertEqual(code, 0)
                self.assertEqual(output.count('Capacity observed.'), 1)
                workflow.apply.assert_not_called()
                if blocked:
                    apply.assert_not_called()
                    self.assertIn('--preflight', output)
                    self.assertNotIn('Apply with:', output)

    def test_replace_plan_runs_interview_and_preflight_without_apply_question(self):
        workflow, questions, apply, code, output = self.run_wizard(flags=['--replace-plan', '--preflight'])
        self.assertEqual(code, 0)
        questions.assert_called_once()
        workflow.plan.assert_called_once_with(interview(), replace=True)
        workflow.preflight.assert_called_once()
        workflow.apply.assert_not_called(); apply.assert_not_called()
        self.assertEqual(output.count('Capacity observed.'), 1)

    def test_replace_rejects_apply_resume_status_and_reset(self):
        for flag in ('--apply', '--resume', '--status', '--reset'):
            args = parser().parse_args(['install', '--ssh', 'anvil', '--replace-plan', flag])
            with self.subTest(flag=flag), self.assertRaises(SetupError): cli_execute(args)

    def test_default_observer_receives_progress_but_injected_observer_contract_stays_three_args(self):
        notify = Mock()
        state = {'version': 1, 'intent': {'ssh': 'anvil'}, 'inputs_digest': digest({'ssh': 'anvil'})}
        for injected in (False, True):
            observer = Mock(return_value={'status': 'preflight'})
            journal = Mock(read=Mock(return_value=deepcopy(state)))
            with patch('forge_client.installation.observe', observer):
                installer = Install('anvil', github=Mock(), installation=Mock(), journal=journal,
                                    notify=notify, **({'preflight': observer} if injected else {}))
                installer.preflight()
            self.assertEqual(observer.call_args.kwargs, {} if injected else {'progress': notify})
            self.assertEqual(len(observer.call_args.args), 3)

    def test_successful_checks_hide_remediation_and_repository_labels_use_names(self):
        from forge_client.installation import display
        lines = []
        display({'status': 'preflight', 'host': {'ssh': 'anvil'}, 'items': [
            {'id': 'local.github', 'status': 'pass', 'detail': 'Signed in.', 'command': ['gh', 'auth', 'login']},
            {'id': 'repository.42', 'repository': 'owner/project', 'status': 'needs_you',
             'detail': 'Check access.', 'command': ['open', 'https://github.com/owner/project']} ]}, tell=lines.append)
        output = '\n'.join(lines)
        self.assertNotIn('gh auth login', output)
        self.assertIn('repository owner/project', output)
        self.assertNotIn('repository.42', output)
        self.assertIn('open https://github.com/owner/project', output)
        self.assertNotIn('Apply with:', output)
