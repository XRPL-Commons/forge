"""Many individually valid requests retain exact evidence beyond the gate size bound."""
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from forge.factory.contracts import assignment_note, result
from forge.factory.result_controller import ResultController
from forge.factory.store_views import PipelineViews
from forge.store.artifacts import Artifacts
from forge.store.executions import Executions, ExecutionQueries, Launch
from forge.store.models import Candidate, TransitionError
from forge.store.transitions import manifest_hash
from forge.store.verification import Verification, VerificationQueries, worker_receipts_in
from forge.store.verification_requests import expand_in, gate_details_in


class CompactVerificationTests(unittest.TestCase):
    def setUp(self):
        from tests.test_task_topology import TaskTopologyTests
        self.f = TaskTopologyTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.store = self.f.store

    def integration(self, count=9):
        f = self.f
        plan = f.plan([(str(i), 'child', f.members, [], []) for i in range(count)])
        requests = []
        for i in range(count):
            key = str(i); reserved, manifest = f.reserve(plan, key)
            job = ExecutionQueries(self.store).job(reserved['job_id'])
            request = {'what': (f'Check {i}: ' + 'x' * 4000)[:4000],
                       'why': (f'Reason {i}: ' + 'y' * 4000)[:4000],
                       'checks': ['image-check'] if i else []}
            commit = hashlib.sha1(key.encode()).hexdigest()
            report = result({'version': 1, 'role': 'build', 'attempt': job['id'],
                'outcome': 'needs_human_verification', 'summary': 'Implemented task',
                'verification': request, 'output_commit': commit, 'delegation_plan': None}, job)
            bundle = b'verified bundle'
            Executions(self.store, clock=lambda: 100).collect(f.command(), job['id'],
                execution_fence=key, capture={'logs': '', 'worktree_bundle': bundle})
            f.fixture.clean(reserved, key)
            accepted = f.delivery.accept_task_output(f.command(), job['id'], execution_fence=key,
                input_manifest=manifest, commit_oid=commit, evidence={'verified': True,
                    'commit_oid': commit, 'base_oid': 'base', 'input_manifest_hash': manifest_hash(manifest),
                    'bundle_digest': hashlib.sha256(bundle).hexdigest(), 'report': report})
            requests.append((accepted['output_id'], request))
        manifest = f.query.builder_manifest('batch', 'integration', base_oid='base', policy_hash='policy')
        reserved = f.delivery.reserve_builder(f.command(), stage='integration', slot='owner',
            execution_fence='integrate', input_manifest=manifest,
            launch=Launch('forge/work', 'builder[bot]', 'codex', 1000, {}, {}))
        return ExecutionQueries(self.store).job(reserved['job_id']), requests

    def publish(self, job, report):
        gate = ResultController._verification(job, report)
        manifest = job['input_manifest']
        candidate = Candidate(8, 'forge/work', 'c' * 40, 'base', 'policy',
                              manifest['requirements_hash'], manifest_hash(manifest))
        f = self.f
        value = f.delivery.publish_candidate(f.command(), job['id'], execution_fence='integrate',
            input_manifest=manifest, candidate=candidate, gates=[gate], evidence={'verified': True,
                'pull_number': 8, 'head_oid': candidate.head_oid, 'base_oid': 'base', 'report': report})
        f.fixture.clean({'job_id': job['id']}, 'integrate')
        with self.store.read_snapshot() as db:
            stored = dict(db.execute("SELECT * FROM unit_gates WHERE candidate_id=? AND code LIKE 'verification_%'",
                                     (value['candidate_id'],)).fetchone())
        return stored

    def test_large_valid_reports_reserve_publish_expand_and_survive_retention(self):
        job, requests = self.integration()
        manifest = job['input_manifest']
        self.assertGreater(sum(len(json.dumps(request)) for _, request in requests), 65536)
        self.assertEqual(len(manifest['verification_requests']), len(requests))
        self.assertTrue(all(set(ref) == {'output_id', 'result_digest'} for ref in manifest['verification_requests']))
        self.assertLess(len(json.dumps(manifest)), 16000)
        note = assignment_note(job)
        self.assertEqual({row['output_id']: row['verification'] for row in note['verification_requests']}, dict(requests))
        final = {'what': 'Final device check', 'why': 'Requires physical hardware', 'checks': []}
        gate = self.publish(job, {'outcome': 'needs_human_verification', 'verification': final})
        self.assertEqual(gate['code'], 'verification_human')
        self.assertLess(len(gate['details_json']), 1000)
        details = VerificationQueries(self.store).gate(gate['id'])['details']
        self.assertEqual({row['output_id']: row['verification'] for row in details['requests']}, dict(requests))
        self.assertEqual(details['final_verification'], final)
        for _, request in requests:
            self.assertIn(request['what'], details['what']); self.assertIn(request['why'], details['why'])
        self.assertEqual(details['checks'], ['image-check'])
        views = PipelineViews(self.store, clock=lambda: 100)
        self.assertEqual(views.issues(key='issue-0')['issue']['verification_request']['what'], details['what'])
        with patch('forge.store.verification_requests.result_in', side_effect=AssertionError('summary hydrated report')):
            self.assertIsNone(views.issues()['issues'][0]['verification_request'])
        Artifacts(self.store).prune_raw_logs(now=40 * 86400)
        self.assertEqual(VerificationQueries(self.store).gate(gate['id'])['details'], details)
        # Confirmation preserves exact expanded context for the subsequent review/audit worker.
        item = VerificationQueries(self.store).gate(gate['id'])
        proof = {key: item['identity'][key] for key in ('head_oid', 'base_oid', 'policy_hash', 'requirements_hash', 'input_manifest_hash')}
        proof.update(candidate_id=item['candidate']['key'], verified=True, observed_at=100,
                     membership_generation=item['unit']['membership_generation'],
                     fencing_generation=item['unit']['fencing_generation'], via='human',
                     operator_confirmed=True, actor_id='operator', statement='All exact requests checked')
        Verification(self.store, clock=lambda: 100).complete_human(self.f.command(), gate['id'], proof=proof)
        with self.store.read_snapshot() as db:
            receipt = worker_receipts_in(db, gate['candidate_id'])[0]
        self.assertEqual(receipt['what'], details['what']); self.assertEqual(receipt['why'], details['why'])
        self.assertEqual(receipt['requests'], details['requests'])

    def test_reference_and_candidate_binding_reject_substitution(self):
        job, _ = self.integration(count=1)
        with self.store.read_snapshot() as db:
            for field, value in [('result_digest', '0' * 64), ('output_id', 'different-output')]:
                manifest = copy.deepcopy(job['input_manifest'])
                manifest['verification_requests'][0][field] = value
                with self.subTest(field=field), self.assertRaises(TransitionError):
                    expand_in(db, manifest, unit_id='batch')
            manifest = copy.deepcopy(job['input_manifest']); manifest['part_outputs'][0]['commit_oid'] = '0' * 40
            with self.assertRaises(TransitionError): expand_in(db, manifest, unit_id='batch')
            with self.assertRaises(TransitionError): expand_in(db, job['input_manifest'], unit_id='different-unit')
        gate = self.publish(job, {'outcome': 'pass'})
        with self.store.read_snapshot() as db:
            for field in ('candidate_id', 'unit_id'):
                with self.subTest(field=field), self.assertRaises(TransitionError):
                    gate_details_in(db, {**gate, field: 'different'})
            changed = {**gate, 'details_json': json.dumps({'verification_job_id': 'different-job'})}
            with self.assertRaises(TransitionError): gate_details_in(db, changed)
            inline = {**gate, 'details_json': json.dumps({'what': 'Legacy check', 'why': 'Legacy reason', 'ci': []})}
            self.assertEqual(gate_details_in(db, inline)['what'], 'Legacy check')
