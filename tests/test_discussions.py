import json
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import uuid
from unittest.mock import Mock, patch

from forge.factory.discussions import Discussions, partial_text, note
from forge.factory.ledger import Ledger
from forge.factory.runner import RunnerError
from forge.factory.config import DEFAULT
from forge.factory.issue_actions import request
from forge.state import AtomicStore


class DiscussionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.row = dict(key='1:2', repo='example/app', repo_id=1, number=2, revision='revision-a',
                        title='Fix tests', body='Required evidence', status='failed', actions=['retry'], dependencies=[])
        self.inventory = Mock(); self.inventory.view.side_effect = lambda **kwargs: {'issue': deepcopy(self.row)}
        self.runner = Mock(); self.runner.inspect.return_value = {'status': 'created', 'running': False}
        self.runner.create.return_value = 'container-id'
        self.factory = Mock(return_value=self.runner)
        self.service = Discussions(self.root, runner=self.factory, inventory=self.inventory, clock=lambda: 1000)
        self.settings = Mock(); self.settings.paused.return_value = False
        self.github = Mock(); self.github.token.return_value = 'read-only-token'
        self.config = {**deepcopy(DEFAULT), 'enabled': True, 'provider_pool': [], 'provider_ready': None}
        Ledger(self.root / 'state/factory').read()

    def send(self):
        identifier = str(uuid.uuid4())
        self.service.send('1:2', 'Why did this fail?', identifier)
        return identifier

    def start(self):
        self.send(); self.service.tick(self.config, self.github, self.settings)
        return self.service.read()['1:2']['job']

    def finish(self, job, **fields):
        self.runner.inspect.return_value = {'status': 'exited', 'running': False}
        result = {'attempt': job['id'], 'role': 'discussion', 'outcome': 'pass', 'summary': 'Fix the shared producer type first.',
                  'proposal': {'action': 'retry', 'reason': 'Fix the shared producer type first.'}, **fields}
        self.runner.capture.return_value = {'result': result}
        self.service.drive(job, self.github)

    def test_send_idempotent_and_never_mutates_pipeline(self):
        before = Ledger(self.root / 'state/factory').read()
        identifier = self.send()
        self.service.send('1:2', 'Why did this fail?', identifier)
        self.assertEqual(len(self.service.view('1:2')['messages']), 1)
        self.assertEqual(before, Ledger(self.root / 'state/factory').read())
        with self.assertRaises(ValueError): self.service.send('1:2', 'A different question', str(uuid.uuid4()))

    def test_read_only_token_and_reconstructed_assignment(self):
        job = self.start()
        self.github.token.assert_called_once_with('recovery', 1, worker=True)
        self.assertEqual(note(job)['role'], 'discussion')
        self.assertIn('NOT a resumed', note(job)['instructions'])
        self.assertEqual(self.service.view('1:2')['status'], 'running')

    def test_the_agent_sees_the_operators_own_buttons(self):
        self.row.update(actions=['retry', 'assign_reviewer'], closable=True, approval={'pr': 31, 'reason': 'Verify the image'})
        job = self.start()
        context = note(job)['context']
        self.assertEqual(context['actions'], ['retry', 'assign_reviewer', 'close'])
        self.assertEqual([a['button'] for a in context['available_actions']], ['Retry attempt', 'Assign reviewer', 'Close issue'])
        self.assertTrue(any('Approve pull request #31 on GitHub' in step for step in context['operator_only']))
        self.assertNotIn("What you can do", note(job)['instructions'])

    def test_the_agent_answers_from_context_first_in_a_short_chat_reply(self):
        text = note(self.start())['instructions']
        # Context first; the repository only when the context cannot answer or the operator asks.
        self.assertIn('Answer FIRST from the supplied context', text)
        self.assertIn('Open the repository only when that context cannot answer the question, or the operator explicitly asks', text)
        self.assertIn('Checking the code to confirm, about 2 min.', text)
        self.assertIn('about 60 seconds of tool use', text)
        # A chat reply: the direct answer first, a hard length limit, no report.
        self.assertIn('2-5 short sentences', text)
        self.assertIn('at most 3 bullets', text)
        self.assertIn('Hard limit: 1200 characters', text)
        self.assertNotIn('12000', text)
        # The buttons are under the reply: never a list of them in prose.
        self.assertIn('Never list the available actions in prose', text)
        self.assertIn('At most one sentence may name the button you recommend', text)
        # The proposal is a note for the next attempt, not the answer again.
        self.assertIn('1-2 sentence note for the next attempt', text)
        self.assertNotIn('Separate observations from inference', text)

    def test_a_long_answer_is_trimmed_not_lost(self):
        job = self.start()
        self.finish(job, summary='The producer type is wrong. ' * 200,
                    proposal={'action': 'retry', 'reason': 'Fix the producer first. ' * 100})
        reply = self.service.view('1:2')['messages'][-1]
        self.assertEqual(reply['role'], 'assistant')
        self.assertLessEqual(len(reply['text']), 1500)
        self.assertTrue(reply['text'].startswith('The producer type is wrong.'))
        self.assertTrue(reply['text'].endswith('…'))
        self.assertLessEqual(len(reply['proposal']['reason']), 400)
        self.assertTrue(reply['proposal']['reason'].endswith('…'))

    def test_a_short_answer_is_kept_as_written(self):
        job = self.start(); self.finish(job)
        reply = self.service.view('1:2')['messages'][-1]
        self.assertEqual(reply['text'], 'Fix the shared producer type first.')
        self.assertEqual(reply['proposal'], {'action': 'retry', 'reason': 'Fix the shared producer type first.'})

    def test_discussion_model_is_used_for_conversations_only(self):
        self.config['discussion_model'] = {'claude': 'claude-haiku-4-5'}
        self.config.update(provider='claude', model='claude-opus-4-1')
        job = self.start()
        self.assertEqual(job['runner_config']['model'], 'claude-haiku-4-5')
        self.assertEqual(self.config['model'], 'claude-opus-4-1')

    def test_without_discussion_model_the_provider_model_stays(self):
        self.config['discussion_model'] = {'grok': 'grok-fast'}
        self.config.update(provider='claude', model='claude-opus-4-1')
        self.assertEqual(self.start()['runner_config']['model'], 'claude-opus-4-1')

    def test_live_names_a_conversation_waiting_or_working(self):
        self.assertFalse(self.service.live())
        self.send()
        self.assertTrue(self.service.live())

    def test_discussions_get_ten_minutes(self):
        job = self.start()
        self.assertEqual(job['deadline'], 1000 + 600)

    def test_complete_then_apply_requires_explicit_operator_and_is_idempotent(self):
        job = self.start(); self.finish(job)
        view = self.service.view('1:2')
        self.assertEqual(view['status'], 'idle')
        self.assertEqual(len(view['messages']), 2)
        with patch('forge.factory.issue_actions.IssueInventory', return_value=self.inventory):
            receipt = self.service.apply('1:2', job['id'])
            self.assertEqual(receipt, self.service.apply('1:2', job['id']))
        with AtomicStore(self.root / 'state/issues', 'actions').transaction() as data:
            self.assertEqual(len(data), 1)
            self.assertEqual(data[receipt]['reason'], 'Fix the shared producer type first.')
            self.assertEqual(data[receipt]['status'], 'pending')

    def test_stale_proposal_cannot_apply(self):
        job = self.start(); self.finish(job)
        self.row['revision'] = 'changed-head'
        with patch('forge.factory.issue_actions.IssueInventory', return_value=self.inventory):
            with self.assertRaisesRegex(ValueError, 'changed'): self.service.apply('1:2', job['id'])

    def test_action_receipt_survives_crash_before_conversation_receipt(self):
        job = self.start(); self.finish(job)
        with patch('forge.factory.issue_actions.IssueInventory', return_value=self.inventory):
            request(self.root, key='1:2', action='retry', revision='revision-a', reason='Fix the shared producer type first.', request_id=job['id'])
            self.row['revision'] = 'changed-after-apply'
            self.assertEqual(self.service.apply('1:2', job['id']), job['id'])

    def test_cancel_is_fenced_and_reconciled_before_replacement(self):
        job = self.start(); self.service.cancel('1:2')
        # A running observation returning after Cancel must not erase the fence.
        self.service.save('1:2', job=job, status='running')
        with self.assertRaises(ValueError): self.service.send('1:2', 'A different question', str(uuid.uuid4()))
        self.service.tick(self.config, self.github, self.settings, launch=False)
        self.assertTrue(self.service.read()['1:2']['job']['cleanup'])
        self.assertEqual(self.service.view('1:2')['status'], 'cancelled')
        self.send()

    def test_reset_cleans_up_even_after_repository_removed(self):
        self.start(); self.inventory.view.side_effect = KeyError('removed')
        self.service.cancel_all()
        job = self.service.active_jobs()[0]
        self.service.drive(job, None)
        self.assertFalse(self.service.active_jobs())

    def test_failed_cleanup_retains_lease(self):
        self.start(); self.service.cancel('1:2')
        self.runner.reset.side_effect = RunnerError('engine unavailable')
        self.service.tick(self.config, self.github, self.settings, launch=False)
        self.assertEqual(len(self.service.active_jobs()), 1)
        with self.assertRaises(ValueError): self.service.send('1:2', 'A different question', str(uuid.uuid4()))
        self.assertEqual(self.service.view('1:2')['presence']['reason'], 'cancelled')

    def test_restart_after_answer_before_cleanup_does_not_duplicate_reply(self):
        job = self.start(); self.runner.reset.side_effect = RunnerError('interrupted cleanup')
        self.finish(job)
        self.runner.reset.side_effect = None
        self.service.tick(self.config, self.github, self.settings, launch=False)
        self.assertEqual(len(self.service.view('1:2')['messages']), 2)
        self.assertEqual(self.service.view('1:2')['status'], 'idle')

    def test_forbidden_proposal_is_not_shown_as_applicable(self):
        # Never a dead end: the answer arrives, the unavailable button does not.
        job = self.start(); self.finish(job, proposal={'action': 'merge', 'reason': 'Skip review'})
        self.assertEqual([m['role'] for m in self.service.view('1:2')['messages']], ['user', 'assistant'])
        reply = self.service.view('1:2')['messages'][-1]
        self.assertEqual((reply['text'], reply['proposal'], reply['unavailable']), ('Fix the shared producer type first.', None, True))
        self.assertEqual(self.service.view('1:2')['status'], 'idle')

    def test_close_is_offered_whenever_the_issue_is_closable_and_in_the_schema(self):
        from forge.factory.harness import role_properties
        self.row.update(actions=['retry'], closable=True)
        job = self.start()
        self.assertEqual(job['context']['actions'], ['retry', 'close'])
        schema = role_properties(note(job))
        self.assertEqual(schema['proposal']['anyOf'][1]['properties']['action']['enum'], ['retry', 'close'])
        self.assertEqual(schema['close_now']['type'], 'boolean')
        self.assertIn('close_now true only when the operator', note(job)['instructions'])
        self.assertEqual(role_properties({**note(job), 'context': {'actions': []}})['proposal'], {'type': 'null'})

    def test_legacy_messages_do_not_grant_unattributed_automatic_close(self):
        self.row.update(closable=True)
        with patch('forge.factory.issue_actions.request', return_value='receipt') as applied:
            identifier = str(uuid.uuid4()); self.service.send('1:2', 'close this issue', identifier)
            self.service.tick(self.config, self.github, self.settings)
            self.finish(self.service.read()['1:2']['job'], proposal={'action': 'close', 'reason': 'Not needed.'}, close_now=True)
            applied.assert_not_called()
            self.assertNotIn('receipt',self.service.view('1:2')['messages'][-1])
        with patch('forge.factory.issue_actions.request', return_value='receipt') as applied:
            self.service.send('1:2', 'What next?', str(uuid.uuid4()))
            self.service.tick(self.config, self.github, self.settings)
            self.finish(self.service.read()['1:2']['job'], proposal={'action': 'close', 'reason': 'Not needed.'}, close_now=True)
            applied.assert_not_called()

    def test_stream_only_contains_public_assistant_text(self):
        events = [{'type': 'assistant', 'message': {'content': [{'type': 'thinking', 'thinking': 'private'}, {'type': 'text', 'text': 'Checking evidence'}, {'type': 'tool_use', 'input': 'secret'}]}},
                  {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'A public answer'}},
                  {'type': 'item.completed', 'item': {'type': 'command_execution', 'output': 'secret'}}]
        self.assertEqual(partial_text('\n'.join(json.dumps(e) for e in events)), 'Checking evidence\nA public answer')

    def test_provider_login_leased_by_pipeline_waits_without_spending_attempt(self):
        self.config['provider_auth_file_env'] = 'AUTH_REF'
        state = Ledger(self.root / 'state/factory').read()
        state['jobs']['other'] = {'issue_key': '1:9', 'created_at': 1, 'cleanup': False, 'runner_config': {'provider_auth_file_env': 'AUTH_REF'}}
        self.send()
        with patch('forge.factory.discussions.Ledger.read', return_value=state):
            self.service.tick(self.config, self.github, self.settings)
        self.runner.start.assert_not_called()
        self.assertEqual(self.service.view('1:2')['status'], 'queued')

    def test_pagination_and_issue_isolation(self):
        with self.service.store.transaction() as data:
            data['1:2'] = {'messages': [{'id': str(i), 'sequence': i, 'role': 'user', 'text': str(i)} for i in range(1, 44)]}
        self.assertEqual(len(self.service.view('1:2')['messages']), 20)
        self.assertEqual(self.service.view('1:2')['before'], 24)
        self.assertEqual(self.service.view('1:2', 24)['messages'][-1]['sequence'], 23)
        self.assertEqual(self.service.view('1:3')['messages'], [])
        with self.assertRaises(ValueError): self.service.view('../private')


if __name__ == '__main__': unittest.main()
