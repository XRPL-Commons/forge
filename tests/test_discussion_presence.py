"""Someone available joins, works and answers, or leaves saying why."""
import json
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import uuid
from unittest.mock import Mock, patch

from forge.factory.discussions import Discussions
from forge.factory.ledger import Ledger
from forge.factory.config import DEFAULT


class PresenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.row = dict(key='1:2', repo='example/app', repo_id=1, number=2, revision='revision-a',
                        title='Fix tests', body='Required evidence', status='failed', actions=['retry'], dependencies=[])
        self.inventory = Mock(); self.inventory.view.side_effect = lambda **kwargs: {'issue': deepcopy(self.row)}
        self.runner = Mock(); self.runner.inspect.return_value = {'status': 'created', 'running': False}
        self.runner.create.return_value = 'container-id'
        self.factory = Mock(return_value=self.runner)
        self.now = 1000.0
        self.service = self.make('boot-a')
        self.settings = Mock(); self.settings.paused.return_value = False
        self.github = Mock(); self.github.token.return_value = 'read-only-token'
        self.config = {**deepcopy(DEFAULT), 'enabled': True, 'provider_pool': [], 'provider_ready': None}
        Ledger(self.root / 'state/factory').read()

    def make(self, boot):
        return Discussions(self.root, runner=self.factory, inventory=self.inventory, clock=lambda: self.now, boot=boot)

    def send(self, text='Why did this fail?'):
        return self.service.send('1:2', text, str(uuid.uuid4()))

    def start(self):
        self.send(); self.service.tick(self.config, self.github, self.settings)
        job = self.service.read()['1:2']['job']
        job['container_id'] = 'container-id'  # The mock reports a created container without create().
        self.service.save('1:2', job=job)
        return job

    def finish(self, job):
        self.runner.inspect.return_value = {'status': 'exited', 'running': False}
        self.runner.capture.return_value = {'result': {'attempt': job['id'], 'role': 'discussion', 'outcome': 'pass',
                                                       'summary': 'Fix the shared producer type first.', 'proposal': None}, 'logs': ''}
        self.service.drive(job, self.github)

    def exited(self, *events, code=1):
        self.runner.inspect.return_value = {'status': 'exited', 'running': False, 'exit_code': code}
        self.runner.capture.return_value = {'result': None, 'logs': logs(*events), 'execution': {'exit_code': code}}

    def pool(self):
        self.config.update(provider_pool=[{'id': 'codex-1', 'provider': 'codex', 'model': None},
                                          {'id': 'claude-1', 'provider': 'claude', 'model': 'claude-sonnet-4-6'}])

    def test_finding_then_joined_with_activity_then_answered(self):
        self.send()
        here = self.service.view('1:2')['presence']
        self.assertEqual((here['phase'], here['waiting_since']), ('finding', 1000.0))
        self.service.tick(self.config, self.github, self.settings)
        here = self.service.view('1:2')['presence']
        self.assertEqual(here['phase'], 'joined')
        self.assertEqual(here['agent'], {'provider': 'codex', 'model': None, 'name': 'Bolt', 'label': 'Codex'})
        self.assertEqual([a['text'] for a in here['activity']], ['Getting started'])
        self.assertEqual(here['mode'], 'evidence')
        job = self.service.read()['1:2']['job']
        self.runner.inspect.return_value = {'status': 'running', 'running': True}
        self.runner.observe.return_value = {'logs': logs(
            {'runner': 'alive', 'attempt': 'x', 'at': 1001},
            {'type': 'item.started', 'item': {'id': 'item_1', 'type': 'command_execution', 'command': "bash -lc 'gh issue view 7 --comments'"}},
            {'type': 'item.completed', 'item': {'id': 'item_1', 'type': 'command_execution', 'aggregated_output': 'SECRET FILE CONTENTS'}},
            {'type': 'item.started', 'item': {'id': 'item_2', 'type': 'command_execution', 'command': 'rg coach src'}})}
        self.now = 1010
        self.service.tick(self.config, self.github, self.settings)
        self.service.tick(self.config, self.github, self.settings)  # The same tail again: no duplicates.
        lines = [a['text'] for a in self.service.view('1:2')['presence']['activity']]
        self.assertEqual(lines, ['Getting started', 'Reading issue #7', 'Searching the repository for `coach`'])
        self.assertNotIn('SECRET', json.dumps(self.service.read()))
        self.assertNotIn('seen', self.service.view('1:2')['presence'])
        # Searching the repository: the operator sees Bolt is checking the code.
        self.assertEqual(self.service.view('1:2')['presence']['mode'], 'code')
        self.finish(job)
        view = self.service.view('1:2')
        self.assertEqual(view['presence']['phase'], 'answered')
        answer = view['messages'][-1]
        self.assertEqual(answer['agent']['name'], 'Bolt')
        self.assertIn('Reading issue #7', [a['text'] for a in answer['activity']])
        # Activity stays in the discussion record, never the factory ledger.
        self.assertNotIn('Reading issue #7', json.dumps(Ledger(self.root / 'state/factory').read()))

    def test_restart_with_the_worker_gone_leaves_with_restart_and_try_again_requeues(self):
        job = self.start()
        self.assertEqual(job['boot'], 'boot-a')
        self.runner.inspect.return_value = None  # The deploy took the container with it.
        self.service = self.make('boot-b')
        self.service.tick(self.config, self.github, self.settings, launch=False)
        view = self.service.view('1:2')
        self.assertEqual((view['presence']['phase'], view['presence']['reason']), ('left', 'restart'))
        self.assertEqual(view['messages'][-1]['role'], 'system')
        self.assertEqual(view['messages'][-1]['text'], 'Bolt left: Forge restarted before the answer was ready')
        self.assertEqual(view['status'], 'unavailable')
        # Try again: the same words requeue the same question, no second copy.
        identifier = view['messages'][0]['id']
        self.assertEqual(self.send(), identifier)
        self.assertEqual(self.service.view('1:2')['presence']['phase'], 'finding')
        self.assertEqual(sum(m['role'] == 'user' for m in self.service.view('1:2')['messages']), 1)

    def test_restart_with_running_worker_is_adopted_and_exit_without_result_is_restart(self):
        job = self.start()
        self.runner.inspect.return_value = {'status': 'running', 'running': True}
        self.runner.observe.return_value = {'logs': ''}
        self.service = self.make('boot-b')
        self.service.drive(job, self.github)
        self.assertEqual(self.service.view('1:2')['presence']['phase'], 'joined')
        job = self.service.read()['1:2']['job']
        self.assertEqual(job['boot'], 'boot-b')
        self.service = self.make('boot-c')
        self.exited(code=137)
        self.service.drive(job, self.github)
        self.assertEqual(self.service.view('1:2')['presence']['reason'], 'restart')

    def test_timeout_says_how_long_and_keeps_the_last_words(self):
        job = self.start()
        self.runner.capture.return_value = {'result': None, 'logs': '{"runner": "alive"}\nlast words'}
        self.now = job['deadline'] + 1
        self.service.tick(self.config, self.github, self.settings, launch=False)
        view = self.service.view('1:2')
        self.assertEqual(view['presence']['reason'], 'timeout')
        self.assertEqual(view['messages'][-1]['text'], 'Bolt ran out of time after 10 min')
        self.assertEqual(self.service.read()['1:2']['job']['excerpt'], 'last words')
        self.assertFalse(self.service.active_jobs())

    def test_stop_is_cancelled_by_you(self):
        self.start(); self.service.cancel('1:2')
        self.service.tick(self.config, self.github, self.settings, launch=False)
        view = self.service.view('1:2')
        self.assertEqual((view['presence']['reason'], view['messages'][-1]['text']), ('cancelled', 'You stopped Bolt'))
        self.assertEqual(view['status'], 'cancelled')

    def test_stop_while_finding(self):
        self.send(); self.service.cancel('1:2')
        view = self.service.view('1:2')
        self.assertEqual((view['presence']['phase'], view['messages'][-1]['text']), ('left', 'You stopped the request'))

    def test_expired_login_says_so_and_switches_once_to_another_provider(self):
        self.pool()
        job = self.start()
        self.assertEqual(job['runner_config']['provider'], 'codex')
        self.exited({'type': 'turn.failed', 'error': {'message': '401 Unauthorized: Provided authentication token is expired. Please try signing in again'}})
        self.now = 1080
        self.service.drive(job, self.github)
        view = self.service.view('1:2')
        left = view['messages'][-1]
        self.assertEqual(left['reason'], 'auth')
        self.assertEqual(left['text'], "Bolt can't sign in: the Codex login expired. Reconnect it in Settings → Coding accounts.")
        self.assertEqual(left['link']['to'], '/settings/harnesses')
        self.assertNotIn('quota', json.dumps(view))
        self.assertIn('401 Unauthorized', self.service.read()['1:2']['job']['excerpt'])
        self.assertEqual((view['status'], view['presence']['phase'], view['presence']['instead_of']['name']), ('queued', 'finding', 'Bolt'))
        self.runner.inspect.return_value = {'status': 'created', 'running': False}
        self.service.tick(self.config, self.github, self.settings)  # Clean up Bolt's worker.
        self.service.tick(self.config, self.github, self.settings)  # Ember joins instead.
        here = self.service.view('1:2')['presence']
        self.assertEqual((here['phase'], here['agent']['name'], here['agent']['model'], here['instead_of']['name']),
                         ('joined', 'Ember', 'claude-sonnet-4-6', 'Bolt'))
        # Only once: a second provider failure leaves for good, with its own cause.
        job = self.service.read()['1:2']['job']
        self.exited({'type': 'error', 'message': 'Provider usage limit reached'})
        self.service.drive(job, self.github)
        view = self.service.view('1:2')
        self.assertEqual((view['status'], view['presence']['reason'], view['messages'][-1]['text']), ('unavailable', 'quota', 'Ember is out of quota'))
        self.assertEqual(view['presence']['until'], 1080 + 300)

    def test_a_lone_provider_that_cannot_sign_in_waits_named_truthfully(self):
        job = self.start()
        self.exited({'type': 'turn.failed', 'error': {'message': '401 Unauthorized: token expired'}})
        self.service.drive(job, self.github)
        self.runner.inspect.return_value = {'status': 'created', 'running': False}
        self.service.tick(self.config, self.github, self.settings)  # Clean up.
        self.service.tick(self.config, self.github, self.settings)  # No one else: keep finding.
        here = self.service.view('1:2')['presence']
        self.assertEqual((here['phase'], here['code'], here['until']), ('finding', 'blocked_auth', 1300))
        self.assertIn("Bolt can't sign in", here['reason'])
        self.assertNotIn('quota', here['reason'])
        self.assertEqual(self.runner.start.call_count, 1)

    def test_cooldown_is_respected_at_launch_and_named(self):
        state = Ledger(self.root / 'state/factory').read()
        state['provider_cooldowns'] = {'codex': {'until': 1900, 'reason': 'blocked_auth'}}
        self.send()
        with patch('forge.factory.discussions.Ledger.read', return_value=state):
            self.service.tick(self.config, self.github, self.settings)
        self.runner.create.assert_not_called()
        here = self.service.view('1:2')['presence']
        self.assertEqual((here['phase'], here['code'], here['until']), ('finding', 'blocked_auth', 1900))
        self.assertIn("Bolt can't sign in", here['reason'])
        self.assertEqual(here['link']['to'], '/settings/harnesses')

    def test_quota_cooldown_skips_to_a_ready_provider(self):
        self.pool()
        state = Ledger(self.root / 'state/factory').read()
        state['provider_cooldowns'] = {'codex': {'until': 1900, 'reason': 'blocked_quota'}}
        self.send()
        with patch('forge.factory.discussions.Ledger.read', return_value=state):
            self.service.tick(self.config, self.github, self.settings)
        self.assertEqual(self.service.view('1:2')['presence']['agent']['name'], 'Ember')

    def test_finding_reasons_and_finding_timeout(self):
        self.send()
        self.settings.paused.return_value = True
        self.service.tick(self.config, self.github, self.settings)
        self.assertEqual(self.service.view('1:2')['presence']['reason'], 'Forge is paused')
        self.settings.paused.return_value = False
        with patch('forge.factory.memory.pending', return_value=[{'key': '1:9'}]):
            self.service.tick(self.config, self.github, self.settings)
        self.assertEqual(self.service.view('1:2')['presence']['code'], 'pipeline')
        self.now = 1000 + 601
        self.service.tick(self.config, self.github, self.settings)
        view = self.service.view('1:2')
        self.assertEqual(view['messages'][-1]['text'], 'No one became available within 10 min: Waiting for pipeline work to launch first')
        self.assertEqual(view['presence']['phase'], 'left')

    def test_another_conversation_holds_the_slot(self):
        self.start()
        self.row['key'] = '1:3'
        self.service.send('1:3', 'And this one?', str(uuid.uuid4()))
        self.runner.inspect.return_value = {'status': 'running', 'running': True}
        self.runner.observe.return_value = {'logs': ''}
        self.service.tick(self.config, self.github, self.settings)
        self.assertEqual(self.service.view('1:3')['presence']['code'], 'busy')

    def test_duplicate_while_pending_is_not_queued_twice(self):
        first = self.send()
        self.assertEqual(self.send('  Why did this fail?  '), first)
        self.service.tick(self.config, self.github, self.settings)
        self.assertEqual(self.send(), first)
        self.assertEqual(len(self.service.view('1:2')['messages']), 1)
        self.assertEqual(self.runner.start.call_count, 1)

    def test_every_unanswered_question_gets_one_left_entry(self):
        job = self.start()
        self.runner.inspect.return_value = None  # Same service: the container vanished.
        self.service.drive(job, self.github)
        view = self.service.view('1:2')
        self.assertEqual((view['presence']['reason'], view['messages'][-1]['text']), ('error', 'Bolt left: its worker container disappeared'))
        # The cleanup tick never adds a second line or rewrites the reason.
        self.service.tick(self.config, self.github, self.settings, launch=False)
        self.assertEqual(sum(m['role'] == 'system' for m in self.service.view('1:2')['messages']), 1)
        self.assertEqual(self.service.view('1:2')['presence']['reason'], 'error')
        self.assertFalse(self.service.active_jobs())

    def test_worker_exit_without_answer_names_how_long(self):
        job = self.start()
        self.now = 1080
        self.exited({'type': 'error', 'message': 'Grok Build did not return a usable result'})
        self.service.drive(job, self.github)
        self.assertEqual(self.service.view('1:2')['messages'][-1]['text'], 'Bolt left: its worker stopped after 1 min without an answer (exit code 1)')

    def test_system_lines_are_not_sent_to_the_agent(self):
        job = self.start(); self.runner.inspect.return_value = None
        self.service.drive(job, self.github)
        self.runner.inspect.return_value = {'status': 'created', 'running': False}
        self.send()
        self.service.tick(self.config, self.github, self.settings, launch=False)
        self.service.tick(self.config, self.github, self.settings)
        self.assertEqual([m['role'] for m in self.service.read()['1:2']['job']['messages']], ['user'])

    def test_conversations_from_before_presence_still_read(self):
        with self.service.store.transaction() as data:
            data['1:2'] = {'messages': [{'id': 'a', 'sequence': 1, 'role': 'user', 'text': 'Hi', 'at': 1}],
                           'status': 'unavailable', 'reason': 'The discussion worker could not complete.'}
        here = self.service.view('1:2')['presence']
        self.assertEqual((here['phase'], here['reason']), ('left', 'error'))


def logs(*events):
    return '\n'.join(json.dumps(e) for e in events)


if __name__ == '__main__': unittest.main()
