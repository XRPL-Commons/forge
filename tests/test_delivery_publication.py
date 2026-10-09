"""Approved ownership transfer never becomes a general publication exemption."""
from copy import deepcopy
import io
import json
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
import uuid
from unittest.mock import patch

from forge.factory.contracts import candidate
from forge.factory.harness import check_publication_target, AssignmentChanged, export_child
from forge.factory.ownership import publication_transfer_matches


class DeliveryPublicationTests(unittest.TestCase):
    def setUp(self):
        self.attempt = str(uuid.uuid4())
        self.author = {'id': 91, 'login': 'maintainer', 'type': 'User'}
        self.target = {'kind': 'existing', 'pull_number': 7, 'author': self.author, 'ownership_transfer': True}
        self.marker = {'version': 2, 'unit': 'batch', 'members': [1, 2], 'attempt': self.attempt, 'head': 'a' * 40}
        self.pr = {'number': 7, 'state': 'open', 'draft': False, 'merged': False, 'user': self.author,
                   'body': 'Fixes #1\nFixes #2\n<!-- forge-submission ' + json.dumps(self.marker) + ' -->',
                   'head': {'sha': 'a' * 40, 'ref': 'topic', 'repo': {'id': 1}},
                   'base': {'sha': 'b' * 40, 'ref': 'main', 'repo': {'id': 1}}}
        self.job = {'id': self.attempt, 'unit_id': 'batch', 'builder_stage': 'integration', 'repo': 'example/repo',
                    'repo_id': 1, 'issue_number': None, 'publisher': 'builder[bot]', 'target_branch': 'main',
                    'branch': 'topic', 'members': [{'issue_number': 1}, {'issue_number': 2}],
                    'input_manifest': {'publication_target': self.target}}

    def test_existing_author_requires_explicit_approved_transfer_and_all_closing_refs(self):
        self.assertEqual(candidate(self.pr, self.job, [], {})['pr'], 7)
        omitted = {**self.pr, 'body': self.pr['body'].replace('Fixes #2\n', '')}
        with self.assertRaises(ValueError): candidate(omitted, self.job, [], {})
        unapproved = deepcopy(self.job); unapproved['input_manifest']['publication_target']['ownership_transfer'] = False
        with self.assertRaises(ValueError): candidate(self.pr, unapproved, [], {})
        with self.assertRaises(ValueError): candidate({**self.pr, 'number': 8}, self.job, [], {})
        stranger = {**self.pr, 'user': {**self.author, 'id': 92}}
        with self.assertRaises(ValueError): candidate(stranger, self.job, [], {})

    def test_inflight_publication_exemption_is_exact_and_not_preupdate_approval(self):
        context = {'publication_transfer': {**self.target, 'expected_head': 'c' * 40,
                   'branch': 'topic', 'base_branch': 'main', 'repository_id': 1, 'delivery_id': 'batch',
                   'members': [1, 2], 'active_integrations': [self.attempt]}}
        self.assertTrue(publication_transfer_matches(self.pr, context))
        self.assertFalse(publication_transfer_matches(self.pr, context, allow_publication=False))
        self.assertFalse(publication_transfer_matches({**self.pr, 'number': 8}, context))
        self.assertFalse(publication_transfer_matches({**self.pr, 'user': {**self.author, 'id': 92}}, context))

    def test_image_checks_target_again_before_running_the_provider(self):
        note = {**self.job, 'resume_pr': 7, 'resume_branch': 'topic', 'resume_head': 'a' * 40,
                'publication_author': self.author}
        with patch('forge.factory.harness.urlopen', return_value=io.BytesIO(json.dumps(self.pr).encode())):
            check_publication_target(note, {'GH_TOKEN': 'synthetic-private-token'})
        changed = deepcopy(self.pr); changed['head']['sha'] = 'd' * 40
        with patch('forge.factory.harness.urlopen', return_value=io.BytesIO(json.dumps(changed).encode())):
            with self.assertRaises(AssignmentChanged): check_publication_target(note, {'GH_TOKEN': 'synthetic-private-token'})

    def test_bad_child_output_is_failure_not_an_environment_hold(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'result.json'
            path.write_text(json.dumps({'outcome': 'pass', 'output_commit': 'b' * 40}))
            git = lambda *args: SimpleNamespace(stdout='a' * 40 + '\n')
            self.assertFalse(export_child({'input_manifest': {'base_oid': 'c' * 40}}, Path(root), [], git, result_path=path))
            report = json.loads(path.read_text())
            self.assertEqual(report['outcome'], 'unable')
            self.assertIsNone(report['output_commit'])

    def test_existing_publication_marker_precedes_push_without_exempting_other_heads(self):
        context = {'publication_transfer': {**self.target, 'expected_head': 'c' * 40,
                   'branch': 'topic', 'base_branch': 'main', 'repository_id': 1, 'delivery_id': 'batch',
                   'members': [1, 2], 'active_integrations': [self.attempt]}}
        before = deepcopy(self.pr); before['draft'] = True; before['head']['sha'] = 'c' * 40
        self.assertTrue(publication_transfer_matches(before, context))
        pushed = deepcopy(self.pr); pushed['draft'] = True
        self.assertTrue(publication_transfer_matches(pushed, context))
        other = deepcopy(pushed); other['head']['sha'] = 'e' * 40
        self.assertFalse(publication_transfer_matches(other, context))
        invalid = deepcopy(pushed)
        invalid['body'] = '<!-- forge-submission ' + json.dumps({**self.marker, 'head': None}) + ' -->'
        self.assertFalse(publication_transfer_matches(invalid, context))
