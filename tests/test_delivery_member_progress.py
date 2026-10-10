"""The lazy delivery API and CLI expose canonical member gates and exact inputs."""
from contextlib import nullcontext, redirect_stdout
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from forge.client_repositories import ClientRepositories
from forge.factory.config import validate
from forge.factory.store_views import PipelineViews
from forge.store.executions import Executions, Launch
from forge.store.models import Command
from forge.store.queries import Queries
from forge_client import cli
from tests import test_delivery_execution as fixtures


class DeliveryMemberProgressTests(unittest.TestCase):
    setUp = fixtures.DeliveryExecutionTests.setUp
    command = fixtures.DeliveryExecutionTests.command
    manifest = fixtures.DeliveryExecutionTests.manifest
    plan = fixtures.DeliveryExecutionTests.plan
    clean = fixtures.DeliveryExecutionTests.clean
    child = fixtures.DeliveryExecutionTests.child
    accept = fixtures.DeliveryExecutionTests.accept

    def prepare(self):
        pins = []
        for number in range(1, 4):
            unit_id, issue_id = f'source-{number}', f'1:{number}'
            admitted = self.life.admit_issue(request_id=f'admit-{number}', actor_id='operator', unit_id=unit_id,
                issue_id=issue_id, repository_id=1, repository_name='example/repo', issue_number=number,
                title=f'Issue {number}', body='Private issue body', requirements={'acceptance': ['works']}, observed_at=90)
            pins.append(dict(issue_id=issue_id, issue_version=1, requirements_revision_id=admitted['revision_id'],
                unit_id=unit_id, unit_version=1, member_id=admitted['member_id']))
        proposal = dict(repository_id=1, base_branch='main', members=pins,
            dependencies=[{'issue_id': '1:2', 'prerequisite_issue_id': '1:1'}], existing_prs=[], rationale='Related change')
        result = self.delivery.compose(Command('compose', 'operator', 'batch', 1), **proposal,
            approval={'approved': True, 'composition_hash': self.delivery.composition_hash(**proposal)})
        self.members = {member['issue_id']: member['member_id'] for member in result['members']}
        self.views = PipelineViews(self.store, runtime=validate({}), clock=lambda: 100)
        return self.plan()

    def running_child(self, plan, key):
        manifest = self.query.child_manifest(plan['plan_id'], key)
        child = self.delivery.delegate(self.command(), plan['plan_id'], key, slot=f'child-{key}',
            execution_fence=f'fence-{key}', input_manifest=manifest, worktree_key=f'worktree-{key}',
            launch=Launch(f'forge/child-{key}', 'builder[bot]', 'codex', 200, {}, {}))
        Executions(self.store, clock=lambda: 100).progress(self.command(), child['job_id'],
            execution_fence=f'fence-{key}', phase='running', container_id=f'container-{key}')
        return child, manifest

    def hold(self):
        for code in ('label_hold', 'environment'):
            self.life.open_gate(self.command(), code, member_id=self.members['1:1'],
                details={'reason': 'private diagnostic must not be exposed'})

    def test_held_dependent_and_running_members_match_issue_status_and_keep_all_gates(self):
        _, plan, _ = self.prepare()
        child, _ = self.running_child(plan, '2')
        self.hold()
        tree = self.views.delivery('1:1')
        members = {member['issue_key']: member for member in tree['members']}
        for key, member in members.items():
            self.assertEqual(member['operator_status'], self.views.issues(key=key)['issue']['operator_status'])
        held, dependent, running = (members[f'1:{number}'] for number in range(1, 4))
        self.assertEqual(held['operator_status']['code'], 'blocked')
        self.assertEqual({gate['code'] for gate in held['gates']}, {'label_hold', 'environment'})
        self.assertEqual(held['blocker'], held['gates'][0])
        self.assertEqual(dependent['operator_status']['reason_code'], 'dependency')
        self.assertEqual(dependent['operator_status']['reason'], 'Waiting for accepted prerequisite outputs')
        self.assertEqual(dependent['gates'][0]['reason'], dependent['operator_status']['reason'])
        self.assertEqual(Queries(self.store).issue('1:2')['lifecycle']['display_status'], dependent['operator_status']['reason'])
        self.assertEqual(dependent['dependencies'], [{'member_id': held['id'], 'issue_key': '1:1', 'number': 1, 'output': None}])
        self.assertEqual(running['operator_status']['code'], 'working')
        self.assertEqual(running['gates'], [])
        self.assertEqual(running['active_job_ids'], [child['job_id']])
        self.assertEqual(held['active_job_ids'], [])
        self.assertNotIn('Private issue body', json.dumps(tree))
        self.assertNotIn('private diagnostic', json.dumps(tree))
        # History pagination cannot change current progress or hide an active child.
        after = max(job['id'] for job in tree['jobs'])
        paged = self.views.delivery('1:1', after=after)
        self.assertEqual(paged['jobs'], [])
        self.assertEqual(paged['members'], tree['members'])

    def test_accepted_output_keeps_exact_dependency_receipt_and_does_not_hide_hold(self):
        _, plan, _ = self.prepare()
        child, manifest = self.child(plan, '0')
        self.accept(child, manifest, '0')
        self.hold()
        tree = self.views.delivery('1:1')
        first, second, _ = tree['members']
        output = next(row for row in self.query.tree('batch')['outputs'] if row['member_id'] == first['id'])
        self.assertEqual(first['output'], output)
        self.assertEqual(first['output_commit'], output['commit_oid'])
        self.assertEqual(second['dependencies'][0]['output'], output)
        self.assertEqual(first['operator_status']['code'], 'blocked')
        self.assertEqual({gate['code'] for gate in first['gates']}, {'label_hold', 'environment'})
        self.assertEqual(second['operator_status']['code'], 'queued')
        self.assertEqual(second['gates'], [])

    def test_dropped_prerequisite_leaves_current_graph_and_becomes_external(self):
        self.prepare()
        removed, dependent = self.members['1:1'], self.members['1:2']
        self.delivery.drop_member(self.command(), removed, membership_generation=1,
            dependent_decisions={dependent: 'wait_external'}, evidence={'approved': True})
        tree = self.views.delivery('1:2')
        self.assertNotIn(removed, {member['id'] for member in tree['members']})
        row = next(member for member in tree['members'] if member['id'] == dependent)
        self.assertEqual(row['dependencies'], [])
        self.assertEqual(row['operator_status']['reason_code'], 'dependency')
        self.assertEqual(row['operator_status']['reason'], 'Waiting for other issues to close')
        self.assertEqual(Queries(self.store).issue('1:2')['lifecycle']['display_status'], row['operator_status']['reason'])
        # Historical edges remain auditable, but the current generation excludes
        # the retired member. The public projection can require exact membership.
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM member_dependencies WHERE prerequisite_member_id=?', (removed,)).fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM member_dependencies d JOIN units u ON u.id=d.unit_id '
                'WHERE d.membership_generation=u.membership_generation AND d.prerequisite_member_id=?', (removed,)).fetchone()[0], 0)

    def test_scoped_client_and_cli_preserve_member_progress_and_exact_references(self):
        _, plan, _ = self.prepare()
        self.running_child(plan, '2')
        self.hold()
        service = SimpleNamespace(settings=SimpleNamespace(root=Path('/unused'), clock=lambda: 100), authorize=Mock())
        target = {'id': 1, 'name': 'example/repo'}
        value = dict(repository=target, instance='instance', generation=1, section='delivery', number=1, page=1, after='')
        with patch('forge.client_repositories.describe', return_value={}), \
             patch('forge.factory.control.public_views', return_value=self.views):
            response = ClientRepositories.read(service, 'token', value)
        self.assertEqual(response['members'], self.views.delivery('1:1')['members'])
        self.assertEqual(service.authorize.call_count, 2)
        output = io.StringIO()
        with patch.object(cli, 'Client') as client, patch.object(cli, 'GitHub') as github, \
             patch.object(cli, 'connection_lock', return_value=nullcontext()), redirect_stdout(output):
            github.return_value.repository.return_value = target
            client.return_value.read.return_value = response
            self.assertEqual(cli.main(['repo', 'delivery', 'example/repo', '1', '--server', 'https://forge.example']), 0)
            client.return_value.read.assert_called_once_with(target, 'delivery', 1, after='')
        self.assertEqual(json.loads(output.getvalue()), {'ok': True, **response})
