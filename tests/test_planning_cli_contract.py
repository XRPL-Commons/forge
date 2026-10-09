"""Agent discovery matches the actual parser and strict server operation boundary."""
from copy import deepcopy
import argparse
import json
import unittest
from unittest.mock import Mock

from forge_client.cli import parser,execute
from forge_client.planning import action_schemas
from forge.factory.planning_service import PlanningService
from forge.store.repository_planning import ACTIONS,NOTE_KINDS,RepositoryPlanning
from forge.store.models import TransitionError


class ReachedService(Exception):pass


class PlanningCLIContractTests(unittest.TestCase):
    def test_export_covers_each_actual_cli_argument_without_changing_capabilities(self):
        cli=parser()
        commands=execute(cli.parse_args(['--json','commands']))['planning_commands']
        exported=json.loads(json.dumps(commands))
        children=next(action for action in cli._actions if isinstance(action,argparse._SubParsersAction)).choices
        for name in ('triage','planning'):
            destinations={action.dest for action in children[name]._actions}-{'help','json'}
            self.assertEqual(set(exported[name]['input_schema']['properties']),destinations)
        self.assertEqual(exported['triage']['input_schema']['required'],['repository','server','request_id'])
        self.assertEqual(exported['planning']['input_schema']['required'],['repository','server'])
        self.assertEqual(exported['triage']['capability'],'repositories:triage')
        self.assertEqual(exported['planning']['read_capability'],'repositories:read')
        self.assertEqual(exported['planning']['write_capability'],'repositories:triage')
        schema=exported['planning']['action_file_schema']
        for action,metadata in exported['planning']['operations'].items():
            index=int(metadata['schema_pointer'].rsplit('/',1)[1])
            self.assertEqual(schema['oneOf'][index]['properties']['action']['const'],action)

    def test_required_action_fields_match_service_and_unknown_fields_are_rejected(self):
        # Stop after the service's exact field guard, before any repository I/O.
        service=Mock();service.read.side_effect=ReachedService()
        for action,schema in action_schemas().items():
            value={key:None for key in schema['required']};value['action']=action
            with self.subTest(action=action),self.assertRaises(ReachedService):
                PlanningService.action(service,1,value)
            self.assertFalse(schema['additionalProperties'])
            self.assertEqual(set(schema['required']),set(schema['properties']))
            for field in schema['required']:
                invalid=dict(value);del invalid[field]
                with self.subTest(action=action,missing=field),self.assertRaises(TransitionError):
                    PlanningService.action(service,1,invalid)
            with self.subTest(action=action,unknown=True),self.assertRaises(TransitionError):
                PlanningService.action(service,1,{**value,'unknown':True})

    def test_row_schemas_cover_actual_actions_and_exact_nested_parameter_fields(self):
        schemas=action_schemas()
        rows=schemas['edit']['properties']['value']['oneOf']
        self.assertEqual({row['properties']['action']['const'] for row in rows},ACTIONS)
        self.assertEqual(set(schemas['note']['properties']['kind']['enum']),NOTE_KINDS)
        source={'url':'https://github.com/example/repo/commit/abc','description':'Evidence','kind':'commit','identity':'abc'}
        params={'close':{'evidence':[source]},'relabel':{'add':['ready'],'remove':[]},'park':{'add':['hold']},
            'group':{'dependencies':[{'issue_id':'one','prerequisite_issue_id':'two'}],'existing_prs':[],'publication_target':{'kind':'new'}},
            'order':{'prerequisites':['example/repo#7']},'question':{'question':'What should change?'},'keep':{}}
        for schema in rows:
            action=schema['properties']['action']['const']
            value={'action':action,'members':['one','two'] if action=='group' else ['one'],'reason':'A reason','rule_quote':None,'parameters':params[action]}
            self.assertEqual(set(value),set(schema['required']))
            self.assertEqual(set(params[action]),set(schema['properties']['parameters']['required']))
            self.assertEqual(RepositoryPlanning._row(value,'')['action'],action)
            invalid=deepcopy(value);invalid['parameters']['unexpected']=True
            with self.assertRaises(TransitionError):RepositoryPlanning._row(invalid,'')
        group=next(row for row in rows if row['properties']['action']['const']=='group')
        self.assertNotIn('maxItems',group['properties']['members'])
        existing=group['properties']['parameters']['properties']['publication_target']['oneOf'][1]
        self.assertEqual(existing['properties']['ownership_transfer'],{'const':True})
        self.assertIn('author',existing['required'])

    def test_limits_and_acceptance_constraints_are_discoverable(self):
        schemas=action_schemas()
        self.assertEqual(schemas['rules']['properties']['rules']['x-max-utf8-bytes'],16384)
        self.assertEqual(schemas['note']['properties']['sources']['maxItems'],20)
        self.assertTrue(schemas['note']['properties']['sources']['items']['properties']['url']['pattern'].startswith('^https://'))
        self.assertEqual(schemas['accept']['anyOf'],[{'properties':{'accept_rows':{'minItems':1}}},{'properties':{'reject_rows':{'minItems':1}}}])
        self.assertIn('disjoint',schemas['accept']['description'])
        self.assertIn('deleted',schemas['note']['required'])
