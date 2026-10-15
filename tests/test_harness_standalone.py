import runpy
import json
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest
from pathlib import Path

from forge.factory.delegation_contracts import PLANNING_OUTCOMES

HARNESS = Path(__file__).resolve().parent.parent / 'forge' / 'factory' / 'harness.py'


class StandaloneHarnessTests(unittest.TestCase):
    """The worker image copies harness.py alone, without the forge package."""

    def test_planning_schema_builds_without_the_package(self):
        harness = runpy.run_path(str(HARNESS))
        note = {'id': 'a', 'role': 'build', 'builder_stage': 'planning', 'input_manifest': {'manifest_version': 2}}
        schema = harness['schema'](note)
        self.assertEqual(schema['properties']['outcome']['enum'], list(PLANNING_OUTCOMES))

    def test_planning_outcomes_match_the_contract(self):
        self.assertEqual(runpy.run_path(str(HARNESS))['PLANNING_OUTCOMES'], PLANNING_OUTCOMES)

    def test_checkout_failure_is_a_private_harness_fault_without_worker_result(self):
        from forge.harnesses.execution import finalize, write_receipt
        from forge.factory.execution_problems import classify, contract_problem
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            note = {'id': 'checkout', 'role': 'build', 'repo': 'example/project'}
            assignment = root / 'assignment.json'; assignment.write_text(json.dumps(note))
            receipt, envelope = root / 'receipt.json', root / 'envelope.json'
            execution = runpy.run_path(str(HARNESS.parent.parent / 'harnesses/execution.py'))
            execution.update(RECEIPT=receipt, write_receipt=lambda *args, **kwargs:
                             write_receipt(*args, **kwargs, path=receipt))
            # Exercise the real clone path, before adapter loading. Neither a
            # failed Git process nor a failed process launch is model output.
            for failure in (SimpleNamespace(returncode=128, stderr='HTTP 503 expected packfile'),
                            OSError('private path or credential')):
                harness = runpy.run_path(str(HARNESS))
                process = Mock(side_effect=failure) if isinstance(failure, Exception) else Mock(return_value=failure)
                with self.subTest(failure=type(failure).__name__), \
                     patch.dict(os.environ, {'FORGE_ASSIGNMENT': str(assignment)}), \
                     patch('subprocess.run', process), \
                     patch('builtins.print'), \
                     patch('runpy.run_path', return_value=execution) as load, \
                     self.assertRaises(SystemExit) as exited:
                    harness['entrypoint']()
                self.assertEqual(exited.exception.code, 125)
                load.assert_called_once_with('/opt/forge/harnesses/execution.py')
                value = finalize(note, 125, receipt_path=receipt, envelope_path=envelope)
                self.assertEqual(value['harness_status'], 'harness_error')
                self.assertEqual(value['harness_error']['exception'], 'PreparationError')
                self.assertNotIn('private', json.dumps(value))
                captured = dict(execution={'id': 'container', 'image_id': 'sha256:'+'a'*64,
                    'exit_protocol': '1', 'running': False, 'exit_code': 125}, execution_envelope=value)
                job = dict(note, container_id='container')
                self.assertEqual(classify(job, captured, 100)['kind'], 'forge_problem')
                self.assertIsNone(contract_problem(job, captured))

    def test_steward_subject_notes_reach_adapter_through_standalone_harness(self):
        from forge.factory.config import DEFAULT
        from forge.factory.contracts import triage_note
        from forge.factory.discussions import note as discussion_note
        from forge.factory.source_inputs import create, assignment
        from forge.mirror.policy import Repository
        from forge.role_qualification_fixtures import Fixtures
        with tempfile.TemporaryDirectory() as temporary:
            for role in ('triage','discussion'):
                with self.subTest(role=role):
                    root=Path(temporary)/role
                    fixtures=Fixtures(root,Repository(1,'acme/project',2,'User',None,'main',True),
                        {'number':10,'title':'Issue context','body':'Read the repository'},
                        'requirements','a'*40,dict(DEFAULT),180)
                    reserved=fixtures.reserve(role)
                    job=dict(reserved)
                    # Production auxiliary jobs have no publisher field; the
                    # note owns their fixed local checkout identity.
                    job.pop('publisher')
                    note=triage_note(job) if role=='triage' else discussion_note(job)
                    manifest={'base_oid':'a'*40}
                    source=create(repository_id=1,canonical_repository='acme/project',base_oid='a'*40,
                        input_oids=[],manifest=manifest,dependency_policy_digest='b'*64,notes=note)
                    frozen=assignment(source,note,manifest=manifest)
                    path=root/'assignment.json';path.write_text(json.dumps(frozen))
                    harness=runpy.run_path(str(HARNESS))
                    run=Mock(return_value=0)
                    git=Mock(return_value=SimpleNamespace(returncode=0,stdout='',stderr=''))
                    with patch.dict(harness['main'].__globals__,{'prepare_environment':lambda *args:{},'begin_agent':Mock()}), \
                         patch.dict(os.environ,{'FORGE_ASSIGNMENT':str(path)}), \
                         patch('subprocess.run',git), \
                         patch('runpy.run_path',return_value={'adapter':lambda provider:{'run':run}}):
                        self.assertEqual(harness['main'](),0)
                    run.assert_called_once()
                    received,_,_,schema=run.call_args.args
                    self.assertEqual(received,frozen)
                    self.assertEqual(schema,job['context']['steward_schema'])
                    commands=[call.args[0] for call in git.call_args_list]
                    self.assertIn(['git','-C','/work/repository','config','user.name','forge-steward'],commands)
                    self.assertIn(['git','-C','/work/repository','config','user.email',
                                   'forge-steward@users.noreply.github.com'],commands)
                    fixtures.fence(reserved)


if __name__ == '__main__':
    unittest.main()
