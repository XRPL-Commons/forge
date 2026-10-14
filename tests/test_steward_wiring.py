from copy import deepcopy
import unittest
from unittest.mock import Mock

from forge.factory.config import validate, DEFAULT
from forge.factory.contracts import assignment_note, result
from forge.factory.harness import schema
from forge.factory.result_controller import ResultController
from forge.factory.source_preparation import SourcePreparation
from forge.store.executions import ExecutionQueries
from forge.steward_contract import decision_schema
from tests import test_steward_core as core_fixture


class StewardWiringTests(unittest.TestCase):
    def setUp(self):
        self.fixture=core_fixture.StewardCoreTests('runTest');self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def assignment(self):
        f=self.fixture;dossier=f.dossier();reserved=f.reserve(dossier)
        job=ExecutionQueries(f.store).job(reserved['id'])
        return dossier,job

    def test_dossier_and_strict_schema_reach_immutable_note_and_result_validation(self):
        dossier,job=self.assignment();note=assignment_note(job)
        self.assertEqual(note['steward']['dossier'],dossier)
        self.assertEqual(schema(note),decision_schema(dossier))
        output=self.fixture.output(dossier)
        self.assertEqual(result(output,job),output)
        wrong=deepcopy(output);wrong['dossier_digest']='0'*64
        with self.assertRaises(ValueError):result(wrong,job)
        note['steward']['dossier']['facts']['charged_failures']=999
        self.assertEqual(job['steward']['dossier']['facts']['charged_failures'],0)
        self.assertIn('Read only',note['instructions'])

    def test_result_controller_leaves_steward_capture_for_its_typed_controller(self):
        _,job=self.assignment();github=Mock()
        controller=ResultController(self.fixture.store,github,policy=lambda job:{},clock=lambda:100)
        self.assertEqual(controller._step(job['id']),{'job_id':job['id'],'pending':'steward_controller'})
        github.assert_not_called()

    def test_steward_source_uses_pinned_notes_without_refreshing_github_inputs(self):
        dossier,job=self.assignment();preparation=object.__new__(SourcePreparation)
        preparation.store=self.fixture.store;preparation.github=Mock();preparation.clock=lambda:100
        preparation.mirror=Mock();preparation.mirror.prepared.return_value=None
        note=assignment_note(job);prepared=preparation._notes(job,note,{})
        self.assertEqual(prepared['steward']['dossier'],dossier)
        self.assertEqual(prepared['steward_evidence'],{'artifacts':[],'notes':[],'truncated':False})
        preparation.github.issue_inputs.assert_not_called()

    def test_recovery_source_keeps_failed_input_revisions_with_current_base(self):
        dossier,job=self.assignment();job['steward']['dossier']['trigger']={'failed_job_id':'failed'}
        preparation=object.__new__(SourcePreparation);preparation.mirror=Mock()
        preparation.mirror.prepared.return_value={'source_input':{'base_oid':'b'*40,'input_oids':['c'*40]},'notes':{'old':'intent'}}
        self.assertEqual(preparation._source_pins(job,job['input_manifest']),('a'*40,['b'*40,'c'*40]))

    def test_explicit_role_defaults_and_legacy_launch_snapshots_are_supported_without_mutation(self):
        self.assertEqual(DEFAULT['slots']['steward'],1)
        legacy={'slots':{'build':4,'review':2,'audit':1,'recovery':2},
                'timeout_seconds':{'build':2700,'review':1200,'audit':1200,'recovery':450}}
        before=deepcopy(legacy);current=validate(legacy)
        self.assertEqual(legacy,before)
        self.assertEqual(current['slots']['steward'],2)
        self.assertEqual(current['timeout_seconds']['steward'],450)
        with self.assertRaises(ValueError):validate({'slots':{'build':1}})
        with self.assertRaises(ValueError):validate({'slots':{**current['slots'],'steward':0}})

    def test_missing_historical_source_is_explicit_and_does_not_read_new_issue_comments(self):
        _,job=self.assignment();job['steward']['dossier']['trigger']={'failed_job_id':'old-failure'}
        preparation=object.__new__(SourcePreparation)
        preparation.store=self.fixture.store;preparation.github=Mock();preparation.clock=lambda:100
        preparation.mirror=Mock();preparation.mirror.prepared.return_value=None
        note=preparation._notes(job,assignment_note(job),{})
        self.assertEqual(note['failed_assignment']['job_id'],'old-failure')
        self.assertTrue(note['failed_assignment']['comments_unavailable'])
        self.assertEqual(note['failed_assignment']['issue_snapshots'][0]['title'],'Keep intent')
        preparation.github.issue_inputs.assert_not_called()
