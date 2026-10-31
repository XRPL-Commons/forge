"""Handover reports preserve evidence boundaries and work with old journals."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge_client.contracts import SetupError, digest
from forge_client.install_report import build, display, storage
from forge_client.installation import Install
from forge_client.workflow import Journal


OLD, REPAIRED = 'a' * 40, 'b' * 40


def completed():
    target = {'id': 10, 'name': 'owner/repository'}
    smoke = {'status': 'handover_complete', 'repository': target, 'number': 3,
        'candidate': {'id': 'candidate', 'pull_number': 4, 'head_sha': 'c' * 40,
                      'base_sha': 'd' * 40, 'merge_sha': 'e' * 40},
        'evidence': {'merge_effect_id': 'merge-effect', 'confirmed_at': 290,
                     'approval_hold_observed': False, 'role_jobs': {'build': 'build-job', 'review': 'review-job', 'audit': 'audit-job'}},
        'attempts': {'total': 2, 'charged_failures': 0},
        'triage': {'id': 'proposal', 'execution_id': 'triage-job', 'row_id': 'row', 'action': 'keep', 'unit_id': 'unit'},
        'started_at': 100, 'merged_at': 290, 'elapsed_seconds': 190, 'verified_at': 299,
        'setup_id': 'setup', 'generation': 'generation', 'intent_digest': 'f' * 64,
        'repositories': [{'target': target, 'merge_policy': 'human', 'allow_workflow_edits': False}]}
    def stage(value, start=110, end=120):
        return {'status': 'complete', 'receipt': value, 'started_at': start, 'completed_at': end}
    state = {'version': 1, 'inputs_digest': 'f' * 64, 'started_at': 100, 'completed_at': 300,
        'intent': {'ssh': 'anvil', 'revision': OLD, 'repositories': smoke['repositories'],
                   'providers': [{'id': 'codex', 'kind': 'browser'}]},
        'preflight': {'observed_at': 280, 'report': {'repositories': [
            {'target': target, 'env_tier': 'standard', 'signed_commits_required': True,
             'classic_protection': False, 'rule_sources': [{'type': 'Organization', 'id': 9,
                 'editable': True, 'enforcement': 'active', 'conditions': {'ignored': 'details'}}]}],
            'items': [{'id': 'github.app_policy', 'status': 'needs_you', 'detail': 'private diagnostic'}]}},
        'waivers': ['github.app_policy'], 'stages': {
            'host': stage({'status': 'installed', 'revision': OLD}),
            'identity': stage({'status': 'identity_ready', 'origin': 'https://anvil.example.ts.net'}),
            'consent': stage({'status': 'consent_ready', 'app_id': 55, 'installation_id': 66,
                             'install_url': 'https://github.com/apps/forge-anvil/installations/new'}),
            'repository.10': stage({'status': 'repository_ready', 'completed': [
                {'kind': 'github.ruleset.repair_baseline', 'ruleset_id': 8,
                 'source_type': 'Repository', 'before': 'f' * 64, 'after': {'rules': []}}]}),
            'account.codex': stage({'status': 'connected'}),
            'publication.10': stage({'status': 'qualified'}),
            'handover': stage(smoke, 290, 300)}}
    state['inputs_digest'] = digest(state['intent'])
    smoke['intent_digest'] = state['inputs_digest']
    return state


class ReportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.journal = Journal('installation', 'anvil', root=Path(temp.name))
        self.state = completed(); self.journal.save(self.state)
        self.host, self.github = Mock(), Mock()
        self.install = Install('anvil', installation=self.host, github=self.github,
                               journal=self.journal, clock=lambda: 310)

    def test_repaired_versions_are_observed_once_without_rewriting_history(self):
        original = self.journal.path.read_bytes()
        with patch('forge_client.version.report', return_value={
                'client_revision': REPAIRED, 'server_revision': REPAIRED}) as versions:
            result = self.install.apply()
            again = self.install.apply()
        versions.assert_called_once_with('anvil')
        self.host.execute.assert_not_called(); self.github.assert_not_called()
        self.assertEqual(self.journal.path.read_bytes(), original)
        self.assertEqual(result['report_path'], str(self.journal.path))
        self.assertEqual(result['stages'], self.state['stages'])
        self.assertEqual(result['handover_report'], again['handover_report'])
        report = json.loads(Path(result['handover_report_path']).read_text())
        self.assertEqual(Path(result['handover_report_path']).stat().st_mode & 0o777, 0o600)
        self.assertEqual(report['versions']['requested_revision'], OLD)
        self.assertEqual(report['versions']['host_install_requested_revision'], OLD)
        self.assertEqual(report['versions']['server_revision'], REPAIRED)
        self.assertEqual(report['versions']['client_revision'], REPAIRED)
        self.assertIsNone(report['versions']['worker_image'])
        self.assertEqual(report['smoke'], self.state['stages']['handover']['receipt'])
        self.assertEqual(report['time_to_first_merge_seconds'], 190)
        self.assertEqual(report['benchmark']['clean_install'], 'not_established')
        self.assertEqual(report['apps']['installation_id'], 66)
        self.assertEqual(report['connect_command'], ['forge', 'connect', 'https://anvil.example.ts.net'])
        self.assertEqual(report['repositories'][0]['rules']['confirmed_changes'][0]['ruleset_id'], 8)

    def test_old_completed_status_is_offline_and_unknowns_are_explicit(self):
        old = deepcopy(self.state)
        old['stages']['consent']['receipt'].pop('installation_id')
        old['stages']['repository.10']['receipt'].pop('completed')
        self.install.state = old
        with patch('forge_client.version.report', side_effect=AssertionError('network read')):
            result = self.install.status()
        self.assertTrue(result['handover_complete'])
        self.assertIsNone(result['handover_report_path'])
        self.assertFalse(storage(self.journal, old).path.exists())
        report = result['handover_report']
        self.assertIsNone(report['versions']['server_revision'])
        self.assertIsNone(report['apps']['installation_id'])
        self.assertEqual(report['repositories'][0]['rules']['change_history'], 'unknown')
        self.assertIn('not a substitute', ' '.join(report['missing_evidence']))

    def test_version_read_failure_keeps_handover_and_does_not_store_error_text(self):
        with patch('forge_client.version.report', side_effect=SetupError('ssh_failed', 'SECRET diagnostic')), \
             patch('forge_client.version.client_revision', return_value=REPAIRED):
            result = self.install.apply()
        self.assertTrue(result['handover_complete'])
        report = result['handover_report']
        self.assertIsNone(report['versions']['server_revision'])
        self.assertEqual(report['versions']['client_revision'], REPAIRED)
        self.assertNotIn('SECRET', Path(result['handover_report_path']).read_text())

    def test_projection_excludes_credentials_codes_free_text_and_raw_rules(self):
        state = deepcopy(self.state)
        for stage in state['stages'].values():
            stage['receipt'].update(secret='SECRET', token='SECRET', code='SECRET',
                                    reason='SECRET', next_action={'command': ['tool', 'SECRET']})
        state['stages']['identity']['receipt']['origin'] += '/callback?token=SECRET#SECRET'
        smoke = state['stages']['handover']['receipt']
        smoke['candidate']['raw'] = 'SECRET'
        smoke['evidence']['role_jobs']['credential'] = 'SECRET'
        smoke['triage']['prompt'] = 'SECRET'
        state['stages']['repository.10']['receipt']['completed'][0]['after'] = {'token': 'SECRET'}
        state['stages']['account.codex']['status'] = 'pending'
        report = build(state)
        self.assertNotIn('SECRET', json.dumps(report))
        self.assertNotIn('private diagnostic', json.dumps(report))
        self.assertEqual(report['origin'], 'https://anvil.example.ts.net')
        self.assertEqual(report['remaining_actions'][0]['stage'], 'account.codex')
        self.assertEqual(report['waived_findings'], [{'id': 'github.app_policy', 'status': 'waived'}])

    def test_readiness_reobservation_preserves_confirmed_rule_changes(self):
        write = self.state['stages']['repository.10']['receipt']
        self.install.state['stages'].pop('repository.10')
        done = lambda row: row.get('status') == 'repository_ready'
        self.install.stage('repository.10', lambda: {'status': 'planned'}, lambda _: write, complete=done)
        self.install.stage('repository.10', lambda: {'status': 'repository_ready'}, complete=done)
        self.install.stage('repository.10', lambda: write, complete=done)
        row = self.install.state['stages']['repository.10']
        self.assertEqual(len(row['rule_changes']), 1)
        report = build(self.install.state)
        self.assertEqual(report['repositories'][0]['rules']['confirmed_changes'], row['rule_changes'])

    def test_same_intent_after_reset_keeps_separate_run_report(self):
        first = storage(self.journal, self.state)
        first.save(build(self.state))
        rerun = deepcopy(self.state)
        rerun['started_at'] = 1000
        second = storage(self.journal, rerun)
        self.assertNotEqual(first.path, second.path)
        self.assertIsNone(second.read())
        self.assertEqual(first.read()['started_at'], 100)

    def test_summary_shows_proof_timings_policy_and_evidence_limits(self):
        lines = []
        display(build(self.state, versions={'client_revision': REPAIRED, 'server_revision': REPAIRED}), tell=lines.append)
        output = '\n'.join(lines)
        for expected in ('owner/repository: human merge', 'codex: connected', 'host: 10 seconds',
                         '/pull/4', '190 seconds', 'includes interruptions', 'not measured',
                         'Connect: forge connect https://anvil.example.ts.net'):
            self.assertIn(expected, output)


if __name__ == '__main__':
    unittest.main()
