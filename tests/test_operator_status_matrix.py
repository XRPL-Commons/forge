"""One matrix drives native projection, scoped client API/CLI and UI assertions."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from forge.client_repositories import ClientRepositories
from forge.factory.config import validate
from forge.factory.store_views import PipelineViews
from forge.store.db import Store
from forge.store.executions import Executions, Launch
from forge.store.models import Candidate, Command
from forge.store.transitions import manifest_hash
from forge_client import cli

MATRIX = json.loads((Path(__file__).parent/'fixtures/operator-status.json').read_text())


class OperatorStatusMatrixTests(unittest.TestCase):
    def command(self):
        self.serial += 1
        with self.store.read_snapshot() as db:
            version = db.execute("SELECT version FROM units WHERE id='u'").fetchone()[0]
        return Command(str(self.serial),'operator','u',version)

    def prepare(self, root, name):
        self.store = Store.create(root)
        self.life = Executions(self.store,clock=lambda:100)
        self.serial = 0
        requirements = {'acceptance':['works']}
        admitted = self.life.admit_issue(request_id='admit',actor_id='operator',unit_id='u',issue_id='1:1',
            repository_id=1,repository_name='owner/repo',issue_number=1,title='Issue',body='Body',
            requirements=requirements,observed_at=90)
        if name in {'working','cleanup_pending','result_pending'}:
            manifest = dict(requirements_revision_id=admitted['revision_id'],base_oid='base',policy_hash='policy')
            job = self.life.reserve_job(self.command(),'build',slot='slot',execution_fence='fence',
                input_manifest=manifest,member_id=admitted['member_id'],
                launch=Launch('forge/issue-1','builder[bot]','codex',200,{},{}))['job_id']
            if name != 'working':
                self.life.collect(self.command(),job,execution_fence='fence',capture={'result':None,'logs':''})
            if name == 'result_pending':
                self.life.verify_cleanup(self.command(),job,execution_fence='fence',evidence={'process_stopped':True,'resources_released':True})
        elif name in {'merged','reopened','approval_required','verification_ci','verification_human'}:
            candidate = Candidate(10,'branch','head','base','policy',manifest_hash(requirements),'inputs')
            adopted = self.life.adopt_candidate(self.command(),candidate,evidence={'approved':True,'verified':True,
                'pull_number':10,'head_oid':'head','base_oid':'base','policy_hash':'policy','input_manifest_hash':'inputs'})
            if name in {'merged','reopened'}:
                self.life.confirm_merged(self.command(),adopted['candidate_id'],merge_sha='merge',evidence={
                    'verified':True,'merged':True,'merge_sha':'merge','pull_number':10,'head_oid':'head','base_oid':'base'})
            else:
                self.life.open_gate(self.command(),name,candidate_id=adopted['candidate_id'],details={'reason':'observed'})
            if name == 'reopened':
                self.observe('open','reopened',requirements)
        elif name in {'completed_elsewhere','dropped'}:
            self.observe('closed','completed' if name == 'completed_elsewhere' else 'not_planned',requirements)
        elif name == 'operator_pause':
            self.life.pause(self.command())
        elif name == 'independent_hold':
            self.life.pause(self.command())
            self.life.open_gate(self.command(),'environment',details={'reason':'database'})
            self.life.resume(self.command())
        elif name != 'queued':
            self.life.open_gate(self.command(),name,details={'reason':'observed','retry_at':200})
        self.views = PipelineViews(self.store,runtime=validate({}),clock=lambda:100)

    def observe(self,state,reason,requirements):
        self.life.observe_issue(self.command(),'1:1',state=state,state_reason=reason,title='Issue',body='Body',
            requirements=requirements,remote_updated_at='2026-10-09T00:00:00Z',generation=1)

    def client_read(self, section):
        service = SimpleNamespace(settings=SimpleNamespace(root=Path('/unused'),clock=lambda:100),authorize=Mock())
        payload = dict(repository={'id':1,'name':'owner/repo'},instance='instance',generation=1,
                       section=section,number=1 if section=='issue' else None,page=1)
        def inventory_view(**kwargs):
            return self.views.issues(**kwargs)
        with patch('forge.client_repositories.describe',return_value={}), \
             patch('forge.factory.control.view',return_value=self.views.factory(issue='1:1')), \
             patch('forge.factory.issues.IssueInventory') as inventory:
            inventory.return_value.view.side_effect = inventory_view
            result = ClientRepositories.read(service,'token',payload)
        # The CLI serializes the scoped API response without another wording map.
        output = io.StringIO()
        with patch.object(cli,'execute',return_value=result), redirect_stdout(output):
            self.assertEqual(cli.main(['repo',section,'owner/repo','--server','https://forge.example',*(['1'] if section=='issue' else [])]),0)
        return json.loads(output.getvalue())

    def test_projection_api_cli_matrix(self):
        for case in MATRIX:
            with self.subTest(case=case['name']), tempfile.TemporaryDirectory() as root:
                self.prepare(root,case['name'])
                rows = [self.views.factory(issue='1:1')['issues'][0], self.views.issues(state='all')['issues'][0],
                        self.views.issues(key='1:1')['issue']]
                for section in ('activity','issues','issue'):
                    response = self.client_read(section)
                    rows.append(response['issue'] if section=='issue' else response['issues'][0])
                for row in rows:
                    self.assertEqual(row['operator_status'],case['expected'])
                    self.assertEqual(row['status'],case['expected']['code'])
                # SQL filtering and public wording must classify the same issue.
                self.assertEqual(self.views.issues(state=case['expected']['code'])['total'],1)
                self.assertEqual(rows[0]['lifecycle'],rows[1]['lifecycle'])
                if case['name']=='independent_hold':
                    with self.store.read_snapshot() as db:
                        self.assertEqual(db.execute("SELECT code FROM unit_gates WHERE resolved_at IS NULL").fetchone()[0],'environment')

    def test_a_github_rule_rejection_has_its_own_reason_and_plain_words(self):
        report = ("Read issue comments; installed locked dependencies. Prepared the CI workflow in local commit 0539aaf9…. "
                  "GitHub rejected publication with GH013: 'refusing to allow a GitHub App to create or update workflow "
                  "`.github/workflows/quality.yml` without `workflows` permission'.")
        cases = [({'reason':'ci_evidence','summary':report}, 'github_permission'),  # an older gate, classified by its text
                 ({'reason':'github_permission','summary':report}, 'github_permission'),
                 ({'reason':'toolchain','summary':'Node 18 is missing; `pnpm` failed.','operator_summary':'Forge could not find the right version of Node. Ask an administrator to update the worker image.'}, 'environment')]
        for details, code in cases:
            with self.subTest(details=details['reason']), tempfile.TemporaryDirectory() as root:
                self.prepare(root,'queued')
                self.life.open_gate(self.command(),'environment',details=details)
                issue = PipelineViews(self.store,runtime=validate({}),clock=lambda:100).issues(key='1:1')['issue']
                self.assertEqual(issue['operator_status']['reason_code'],code)
                self.assertEqual(issue['status'],'attention')
                # The technical report stays available for the details toggle.
                self.assertEqual(issue['environment_blocker'],details['summary'])
                if code == 'github_permission':
                    self.assertEqual(issue['summary']['headline'],"GitHub won't let Forge change workflow files in this repository")
                    self.assertEqual(issue['operator_status']['reason'],"GitHub won't let Forge change workflow files in this repository")
                    self.assertIn('Workflows permission',issue['summary']['plain'])
                    self.assertIn('close the issue',issue['summary']['plain'])
                else:
                    self.assertEqual(issue['summary']['plain'],details['operator_summary'])
                for residue in ('GH013','0539aaf9','`','CI '):
                    self.assertNotIn(residue,issue['summary']['plain'])

