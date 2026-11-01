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

    def server_completed(self):
        state = deepcopy(self.state)
        state['stages'].pop('account.codex')
        state['stages'].pop('publication.10')
        state['stages']['repository.10']['status'] = 'pending'
        selected = [{**row, 'github_protection': 'required'} for row in state['intent']['repositories']]
        plan = {'intent_digest': state['inputs_digest'], 'started_at': state['started_at'],
                'repositories': selected, 'smoke_repository': {'id': 10, 'name': 'owner/repository'},
                'providers': state['intent']['providers']}
        state['server_preparation'] = {'registered': True, 'plan': plan}
        handover = state['stages']['handover']['receipt']
        handover['repositories'] = selected
        value = {**plan, 'available': True, 'ready': True, 'status': 'preparation_ready',
                 'setup_id': handover['setup_id'], 'generation': handover['generation'],
                 'providers': [{'id': 'codex', 'kind': 'browser', 'connected': True}],
                 'stages': [
                     {'id': 'environment', 'status': 'complete'},
                     {'id': 'account.codex', 'status': 'complete', 'started_at': 120, 'completed_at': 150},
                     {'id': 'repository.10', 'status': 'complete', 'started_at': 125, 'completed_at': 160},
                     {'id': 'publication.10', 'status': 'complete', 'started_at': 160, 'completed_at': 200}]}
        state['stages']['preparation'] = {'status': 'complete', 'receipt': value, 'started_at': 120, 'completed_at': 200}
        return state

    def test_server_handover_projects_status_and_timings_without_losing_local_rule_changes(self):
        state = self.server_completed()
        report = build(state)
        self.assertEqual(report['status'], 'handover_complete')
        self.assertEqual(report['providers'][0]['status'], 'connected')
        repository = report['repositories'][0]
        self.assertEqual(repository['stage_status'], 'complete')
        self.assertEqual(repository['publication_status'], 'qualified')
        self.assertEqual(repository['rules']['confirmed_changes'][0]['ruleset_id'], 8)
        self.assertEqual(repository['rules']['change_history'], 'recorded')
        timings = {row['stage']: row for row in report['stages']}
        self.assertEqual(timings['account.codex']['elapsed_seconds'], 30)
        self.assertEqual(timings['repository.10']['elapsed_seconds'], 35)
        self.assertEqual(timings['publication.10']['elapsed_seconds'], 40)
        self.assertIsNone(timings['environment']['started_at'])
        self.assertIsNone(timings['environment']['completed_at'])
        self.assertIsNone(timings['environment']['elapsed_seconds'])
        self.assertEqual(report['remaining_actions'], [])
        self.assertEqual(state['stages']['repository.10']['status'], 'pending')

    def test_server_report_omits_codes_raw_errors_and_unrecognized_stages(self):
        state = self.server_completed()
        prepared = state['stages']['preparation']['receipt']
        prepared.update(code='SECRET', url='https://auth.example?token=SECRET', error='SECRET')
        prepared['providers'][0].update(code='SECRET', token='SECRET')
        for stage in prepared['stages']:
            stage.update(label='SECRET', error='SECRET', reason='SECRET', next_action={'command': ['SECRET']})
        prepared['stages'].append({'id': 'SECRET', 'status': 'complete', 'started_at': 10, 'completed_at': 20})
        prepared['stages'][0].update(started_at='SECRET', completed_at=float('nan'))
        report = build(state)
        self.assertNotIn('SECRET', json.dumps(report, allow_nan=False))
        self.assertEqual(report['providers'][0]['status'], 'connected')
        self.assertEqual(report['repositories'][0]['publication_status'], 'qualified')

    def test_mismatched_server_receipt_cannot_supply_report_evidence(self):
        for key in ('intent_digest', 'started_at', 'repositories', 'smoke_repository', 'setup_id', 'generation'):
            with self.subTest(key=key):
                state = self.server_completed()
                state['stages']['preparation']['receipt'][key] = None
                report = build(state)
                self.assertEqual(report['providers'][0]['status'], 'unknown')
                self.assertEqual(report['repositories'][0]['stage_status'], 'pending')
                self.assertEqual(report['repositories'][0]['publication_status'], 'unknown')
                self.assertFalse(any(row['stage'] == 'account.codex' for row in report['stages']))

    def test_server_report_does_not_infer_connection_or_rule_history(self):
        state = self.server_completed()
        prepared = state['stages']['preparation']['receipt']
        state['stages'].pop('repository.10')
        prepared['providers'] = []
        report = build(state)
        self.assertEqual(report['providers'][0]['status'], 'unknown')
        self.assertEqual(report['repositories'][0]['rules']['change_history'], 'unknown')
        self.assertEqual(report['repositories'][0]['rules']['confirmed_changes'], [])
        prepared['providers'] = [{'id': 'codex', 'kind': 'browser', 'connected': False}]
        self.assertEqual(build(state)['providers'][0]['status'], 'not_connected')

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
