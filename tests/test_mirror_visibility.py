import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from forge.settings import digest, main
from forge.store.mirror import MirrorStore, view_in
from forge.store.job_evidence import publication_in
from forge.factory.source_preparation import SourcePreparation
from tests import test_mirror_service, test_repository_setup, test_settings_http, test_store_recovery


class MirrorViewsTests(unittest.TestCase):
    setUp = test_mirror_service.StoreTests.setUp

    def test_view_is_bounded_safe_and_keeps_exact_refusal_identity(self):
        for index in range(25):
            self.store.append_receipt(self.session_id, dict(outcome='denied', reason='project_denied',
                repository='acme/secret', repository_id=37, requested_oids=['a' * 40] * 50))
        with self.db.read_snapshot() as db:
            view = view_in(db, 'aux:triage:1')
        self.assertEqual(view['claims_total'], 25)
        self.assertEqual(len(view['claims']), 20)
        self.assertTrue(view['receipts_truncated'])
        self.assertEqual(view['claims'][0]['repository_id'], 37)
        self.assertEqual(view['claims'][0]['reason'], 'project_denied')
        self.assertNotIn('requested_oids', json.dumps(view))
        self.assertNotIn('execution_fence', json.dumps(view))
        self.assertLess(len(json.dumps(view)), 30000)


class MirrorSettingsTests(unittest.TestCase):
    setUp = test_repository_setup.RepositorySetupTests.setUp

    def test_operator_revision_and_restrictions_preserved_across_disable(self):
        revision = digest(self.settings.load())
        entries = [{'canonical_name': 'Sensitive/Private', 'repository_id': 22}]
        result = self.service.mirror_policy(10, revision, entries)
        self.assertEqual(result['repositories'][0]['mirror_denied_repositories'],
                         [{'canonical_name': 'sensitive/private', 'repository_id': 22}])
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.service.mirror_policy(10, revision, [])
        self.service.disable(10, result['revision'])
        self.assertEqual(self.settings.load()['repositories'][0]['mirror_denied_repositories'][0]['repository_id'], 22)
        result = self.service.mirror_policy(10, digest(self.settings.load()), [])
        self.assertEqual(result['repositories'][0]['mirror_denied_repositories'], [])
        self.github.assert_not_called()

    def test_invalid_denies_and_unknown_project_do_not_change_configuration(self):
        before = self.settings.load()
        for entries in ([{'canonical_name': 'https://token@github.com/acme/private'}],
                        [{'canonical_name': 'acme/private', 'repository_id': True}],
                        [{'canonical_name': 'acme/private'}, {'canonical_name': 'ACME/private'}]):
            with self.assertRaises(ValueError):
                self.service.mirror_policy(10, digest(before), entries)
        with self.assertRaises(ValueError):
            self.service.mirror_policy(99, digest(before), [])
        self.assertEqual(before, self.settings.load())

    def test_host_cli_requires_revision_and_uses_same_policy_service(self):
        with patch('forge.settings.Settings', return_value=self.settings), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['mirror', '--root', str(self.root), '--repo-id', '10']), 0)
            revision = json.loads(output.getvalue())['revision']
            self.assertEqual(main(['mirror', '--root', str(self.root), '--repo-id', '10', '--revision', revision,
                                  '--denied-json', '[{"canonical_name":"acme/private"}]']), 0)
        self.assertEqual(self.settings.load()['repositories'][0]['mirror_denied_repositories'], [{'canonical_name':'acme/private'}])


class MirrorHTTPTests(unittest.TestCase):
    setUp = test_settings_http.SettingsHTTPTests.setUp
    handler = test_settings_http.SettingsHTTPTests.handler

    def test_operator_endpoint_requires_csrf_and_exact_payload(self):
        value = dict(repo_id=10, revision='current', denied_repositories=[])
        handler = self.handler('/api/repositories/mirror', value, Origin='http://attacker.example')
        handler.do_POST(); handler.send_error.assert_called_once_with(403)
        with patch('forge.repository_setup.RepositorySetup') as service:
            service.return_value.mirror_policy.return_value = {'revision':'new','repositories':[]}
            handler = self.handler('/api/repositories/mirror', value); handler.do_POST()
            service.return_value.mirror_policy.assert_called_once_with(**value)
            self.assertEqual(json.loads(handler._send.call_args.args[0])['revision'], 'new')
            service.return_value.mirror_policy.reset_mock()
            handler = self.handler('/api/repositories/mirror', {**value, 'grant':True}); handler.do_POST()
            service.return_value.mirror_policy.assert_not_called()
            self.assertFalse(json.loads(handler._send.call_args.args[0])['ok'])


class RecoveryEvidenceTests(unittest.TestCase):
    setUp = test_store_recovery.RecoveryStoreTests.setUp
    view = test_store_recovery.RecoveryStoreTests.view
    command = test_store_recovery.RecoveryStoreTests.command
    launch = test_store_recovery.RecoveryStoreTests.launch
    capture_cleanup = test_store_recovery.RecoveryStoreTests.capture_cleanup
    fail = test_store_recovery.RecoveryStoreTests.fail
    reserve_recovery = test_store_recovery.RecoveryStoreTests.reserve_recovery

    def test_recovery_sees_original_claim_and_old_issue_notes_without_fetching_current_issue(self):
        failed = self.fail()
        mirror = MirrorStore(self.store, clock=lambda:100)
        mirror.preparation_receipt(dict(job_id=failed['id'], manifest=failed['input_manifest'], execution_fence=failed['execution_fence']),
            dict(repository='secret/project', repository_id=77, outcome='denied', reason='installation_access_missing'))
        recovery = self.reserve_recovery(failed)
        self.assertEqual(recovery['failure']['mirror']['claims'][0]['repository'], 'secret/project')
        preparation = object.__new__(SourcePreparation)
        preparation.store, preparation.mirror = self.store, mirror
        preparation.github = Mock()
        preparation.github.issue_inputs.side_effect = AssertionError('Recovery must not require current issue requirements')
        preparation.clock = lambda:100
        result = preparation._notes(recovery, {'diagnosis':'original failure'}, {})
        self.assertEqual(result['failed_assignment']['job_id'], failed['id'])
        self.assertTrue(result['failed_assignment']['comments_unavailable'])
        frozen = dict(source_input={'original':'source'}, notes={'issue_observations':[{'body':'before change'}]})
        with patch.object(mirror, 'prepared', return_value=frozen):
            result = preparation._notes(recovery, {}, {})
        self.assertEqual(result['failed_assignment']['notes'], frozen['notes'])
        preparation.github.issue_inputs.assert_not_called()

    def test_publication_confirmed_intent_supersedes_preintent_claim(self):
        failed = self.fail()
        with self.store.write() as db:
            payload = dict(job_id=failed['id'], claim={'code':'unsupported', 'paths':['a']*50, 'worker_secret':'not projected'})
            db.execute("INSERT INTO decisions(id,request_id,unit_id,actor_id,command,status,payload_json,created_at) VALUES('claim','claim','u','host','publication_claim','applied',?,100)", (json.dumps(payload),))
            view = publication_in(db, failed['id'])
            self.assertFalse(view['active'])
            self.assertEqual(len(view['claim']['paths']), 10)
            self.assertNotIn('worker_secret', view['claim'])
            db.execute("INSERT INTO publication_intents VALUES('pub',?,'u','f','{}',?,'{}','{}','{}','confirmed',NULL,1,100,101)", (failed['id'], 'a'*64))
            view = publication_in(db, failed['id'])
        self.assertEqual(view['status'], 'confirmed')
        self.assertIsNone(view['claim'])

    def test_recovery_reuses_failed_source_pins_including_resumed_heads(self):
        preparation = object.__new__(SourcePreparation)
        preparation.mirror = Mock()
        preparation.mirror.prepared.return_value = dict(source_input={'base_oid':'a'*40, 'input_oids':['b'*40]})
        base, inputs = preparation._source_pins({'role':'recovery', 'failure':{'attempt':'failed'},
            'candidate':{'head':'c'*40}}, {'base_oid':'d'*40})
        self.assertEqual((base, inputs), ('a'*40, ['b'*40]))
        preparation.mirror.prepared.assert_called_once_with('failed')

    def test_resume_head_is_an_explicit_input_pin(self):
        base, inputs = SourcePreparation._pins({'resume_head':'b'*40}, {'base_oid':'a'*40})
        self.assertEqual(base, 'a'*40)
        self.assertEqual(inputs, ['b'*40])
