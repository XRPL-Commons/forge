"""Provider routing is local, globally alternating, and conservative on uncertain leases."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from forge import routing
from forge.lifecycle import generation
from forge.state import StateError

ROOT = Path(__file__).parents[1]


def identity(number, born=100):
    return generation(f'${number}|{born}|%{number}|{1000+number}')


class RoutingTransitionsTests(unittest.TestCase):
    def test_global_alternation_follows_successful_binding_across_all_roles(self):
        data = {}
        for index, session in enumerate(('one-build','two-audit','three-review','four-build')):
            selected = routing.choose(data, {'claude','codex'}, session, 200+index)
            expected = 'claude' if index % 2 == 0 else 'codex'
            self.assertEqual(selected['provider'], expected)
            routing.bind(data, session, expected, identity(index+1), 200+index)
        self.assertEqual(data['last_provider'], 'codex')

    def test_three_harnesses_rotate_equally_and_skip_blocked_or_unavailable(self):
        self.assertEqual(routing.PROVIDERS, ('claude','codex','grok'))
        data = {}
        expected = ['claude','codex','grok','claude']
        for index, session in enumerate(('one-build','two-audit','three-review','four-build')):
            selected = routing.choose(data, set(routing.PROVIDERS), session, 200+index)
            self.assertEqual(selected['provider'], expected[index])
            self.assertEqual(selected['reason'], 'alternating_dispatch')
            routing.bind(data, session, expected[index], identity(index+1), 200+index)
        # Codex is next; a Grok outage then sends the following turn to Claude, not back to Codex.
        self.assertEqual(routing.choose(data, set(routing.PROVIDERS), 'five-audit', 300)['provider'], 'codex')
        routing.bind(data, 'five-audit', 'codex', identity(5, 300), 301)
        self.assertTrue(routing.fail(data, 'three-review', 'grok', 'authentication', identity(3), 302))
        selected = routing.choose(data, set(routing.PROVIDERS), 'six-build', 303)
        self.assertEqual((selected['provider'], selected['reason'], selected['degraded']), ('claude', 'alternate_provider_available', True))
        # An unconfigured optional harness is simply absent from the rotation.
        routing.bind(data, 'six-build', 'claude', identity(6, 303), 304)
        self.assertEqual(routing.choose(data, {'claude','codex'}, 'seven-review', 305)['provider'], 'codex')
        # A requested but unavailable harness is skipped rather than forced.
        data = {'next_provider': 'grok'}
        self.assertEqual(routing.choose(data, {'claude','codex'}, 'one-build', 400)['provider'], 'claude')

    def test_optional_harness_requires_explicit_executable_and_reports_readiness(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary)/'grok'
            binary.write_text('#!/bin/sh\nexit 0\n'); binary.chmod(0o755)
            with patch.dict(os.environ, {'FORGE_GROK_BIN': '', 'FORGE_CLAUDE_BIN': '', 'FORGE_CODEX_BIN': ''}), \
                    patch.object(routing.shutil, 'which', return_value=str(binary)):
                self.assertIsNone(routing.executable('grok'))
                self.assertEqual(routing.executable('claude'), str(binary))
                view = routing.readiness({}, now=10)
                self.assertEqual(view['grok']['status'], 'not_configured')
                self.assertIn('grok login --device-code', view['grok']['reconnect'])
                self.assertEqual(view['claude']['status'], 'ready')
            with patch.dict(os.environ, {'FORGE_GROK_BIN': str(binary)}):
                self.assertEqual(routing.executable('grok'), str(binary))
                view = routing.readiness({'providers': {'claude': {'reason': 'blocked_auth', 'blocked_until': 500, 'last_failure': 1}}}, now=10)
                self.assertEqual((view['grok']['status'], view['grok']['qualified'], view['grok']['dispatchable']), ('unqualified', False, False))
                self.assertIn('qualification', view['grok']['note'])
                self.assertEqual((view['claude']['status'], view['claude']['reason'], view['claude']['blocked_until']), ('unavailable', 'blocked_auth', 500))
                self.assertIn('retry --provider claude', view['claude']['reconnect'])
                self.assertEqual(routing.readiness({'providers': {'claude': {'blocked_until': 5}}}, now=10)['claude']['status'], 'ready')

    def test_requested_next_provider_waits_for_availability_and_binding(self):
        data={'next_provider':'codex'}
        routing.fail(data,'old-review','codex','usage_limit',identity(1),100)
        self.assertEqual(routing.choose(data,{'claude','codex'},'one-build',200)['provider'],'claude')
        routing.bind(data,'one-build','claude',identity(2),201)
        self.assertEqual(data['next_provider'],'codex')
        self.assertEqual(routing.choose(data,{'claude','codex'},'two-audit',3701)['provider'],'codex')
        routing.bind(data,'two-audit','codex',identity(3,3701),3702)
        self.assertNotIn('next_provider',data)

    def test_failed_selection_does_not_consume_global_alternation(self):
        data = {}
        self.assertEqual(routing.choose(data, {'claude','codex'}, 'one-build', 100)['provider'],'claude')
        self.assertEqual(routing.choose(data, {'claude','codex'}, 'two-review', 101)['provider'],'claude')
        routing.bind(data,'two-review','claude',identity(2),102)
        self.assertEqual(routing.choose(data, {'claude','codex'}, 'one-build',103)['provider'],'codex')

    def test_genuine_usage_and_authentication_only_open_provider_outages(self):
        for reason, expected in [('usage_limit','blocked_model_usage'),('blocked_quota','blocked_quota'),('authentication','blocked_auth')]:
            with self.subTest(reason=reason):
                data={}
                self.assertTrue(routing.fail(data,'one-build','claude',reason,identity(1),200))
                self.assertEqual(data['providers']['claude']['reason'],expected)
                self.assertEqual(data['providers']['claude']['blocked_until'],3800)
                self.assertEqual(routing.choose(data,{'claude','codex'},'two-review',3799)['provider'],'codex')
        for reason in ('permission','rate_limit','execution_failed','security_audit_failed','no_progress','test_failed'):
            with self.subTest(reason=reason):
                data={}
                self.assertFalse(routing.fail(data,'one-build','claude',reason,identity(1),200))
                self.assertEqual(data,{})

    def test_both_down_preserves_bindings_and_does_not_claim_dispatch(self):
        data={}
        routing.bind(data,'one-build','claude',identity(1),100)
        routing.fail(data,'one-build','claude','usage_limit',identity(1),200)
        routing.fail(data,'two-review','codex','usage_limit',identity(2),250)
        before=copy.deepcopy(data['bindings'])
        result=routing.choose(data,{'claude','codex'},'three-audit',300)
        self.assertIsNone(result['provider'])
        self.assertEqual(result['retry_at'],3800)
        self.assertEqual(data['bindings'],before)
        self.assertEqual(data['last_provider'],'claude')

    def probing(self):
        data={}
        routing.fail(data,'old-build','claude','usage_limit',identity(1),100)
        selected=routing.choose(data,{'claude'},'one-review',3701)
        self.assertTrue(selected['probe'])
        routing.bind(data,'one-review','claude',identity(2,3701),3702)
        return data

    def test_expired_outage_allows_one_real_probe_and_live_generation_keeps_lease(self):
        data=self.probing()
        self.assertIsNone(routing.choose(data,{'claude'},'two-audit',3703)['provider'])
        self.assertIsNone(routing.choose(data,{'claude'},'two-audit',4000,
                                        lambda session:identity(2,3701))['provider'])
        self.assertEqual(data['providers']['claude']['probe_session'],'one-review')

    def test_slow_probe_start_binds_live_ownership_after_initial_lease_expires(self):
        data={}
        routing.fail(data,'old-build','claude','usage_limit',identity(1),100)
        routing.choose(data,{'claude'},'one-review',3701)
        routing.bind(data,'one-review','claude',identity(2,3702),4000)
        self.assertIsNone(routing.choose(data,{'claude'},'two-audit',4001,
                                        lambda session:identity(2,3702))['provider'])

    def test_unknown_probe_generation_never_means_dead_after_lease_expiry(self):
        for lookup in (None, lambda session:None):
            with self.subTest(lookup=lookup):
                data=self.probing()
                self.assertIsNone(routing.choose(data,{'claude'},'two-audit',4000,lookup)['provider'])

    def test_confirmed_absence_or_replacement_allows_one_new_probe(self):
        for observed in (False, identity(3,3990)):
            with self.subTest(observed=observed):
                data=self.probing()
                self.assertEqual(routing.choose(data,{'claude'},'two-audit',4000,lambda s:observed)['provider'],'claude')
                self.assertNotIn('probe_generation',data['providers']['claude'])
                self.assertIsNone(routing.choose(data,{'claude'},'three-build',4001,lambda s:observed)['provider'])

    def test_duplicate_failure_does_not_extend_outage(self):
        data={}
        routing.fail(data,'one-build','claude','usage_limit',identity(1),100)
        self.assertFalse(routing.fail(data,'one-build','claude','blocked_model_usage',identity(1),900))
        self.assertEqual(data['providers']['claude']['blocked_until'],3700)

    def test_replacement_failure_before_bind_requires_current_matching_selection(self):
        data={}
        routing.bind(data,'one-build','claude',identity(1),100)
        routing.choose(data,{'claude','codex'},'one-build',201)
        self.assertTrue(routing.fail(data,'one-build','codex','usage_limit',identity(2,200),202))
        self.assertEqual(data['providers']['codex']['blocked_until'],3802)
        for selected in (None, {'provider':'claude','selected_at':201}, {'provider':'codex','selected_at':99}):
            with self.subTest(selected=selected):
                bad={};routing.bind(bad,'one-build','claude',identity(1),100)
                if selected: bad['selections']={'one-build':selected}
                with self.assertRaises(ValueError):
                    routing.fail(bad,'one-build','codex','usage_limit',identity(2,200),202)

    def test_same_generation_cannot_change_provider(self):
        data={};routing.bind(data,'one-build','claude',identity(1),100)
        data['selections']={'one-build':{'provider':'codex','selected_at':201}}
        with self.assertRaises(ValueError): routing.bind(data,'one-build','codex',identity(1),202)
        with self.assertRaises(ValueError): routing.fail(data,'one-build','codex','usage_limit',identity(1),202)

    def test_success_from_older_parallel_execution_cannot_clear_outage(self):
        data={}
        routing.bind(data,'one-build','claude',identity(1),100)
        routing.bind(data,'two-review','claude',identity(2),101)
        routing.fail(data,'one-build','claude','usage_limit',identity(1),200)
        self.assertFalse(routing.success(data,'two-review',identity(2),300))
        self.assertFalse(routing.success(data,'one-build',identity(9),300))
        self.assertIn('claude',data['providers'])
        routing.choose(data,{'claude'},'three-audit',3801)
        routing.bind(data,'three-audit','claude',identity(3,3801),3802)
        self.assertTrue(routing.success(data,'three-audit',identity(3,3801),3803))
        self.assertNotIn('claude',data['providers'])

    def test_existing_session_gate_respects_outage_and_exclusive_probe_owner(self):
        first, second=identity(1),identity(2)
        data={}
        self.assertTrue(routing.allowed(data,'one-build','claude',first,100))
        routing.bind(data,'one-build','claude',first,100)
        self.assertFalse(routing.allowed(data,'one-build','codex',first,101))
        routing.fail(data,'one-build','claude','usage_limit',first,200)
        self.assertFalse(routing.allowed(data,'one-build','claude',first,300))
        self.assertFalse(routing.allowed(data,'two-review','claude',second,300))
        self.assertFalse(routing.allowed(data,'two-review','claude',second,3801))
        routing.choose(data,{'claude'},'two-review',3801)
        self.assertTrue(routing.allowed(data,'two-review','claude',second,3802))
        self.assertFalse(routing.allowed(data,'one-build','claude',first,3802))
        routing.bind(data,'two-review','claude',second,3802)
        self.assertFalse(routing.allowed(data,'two-review','claude',identity(9,3802),3803))
        self.assertTrue(routing.allowed(data,'two-review','claude',second,4100))
        self.assertFalse(routing.allowed(data,'one-build','claude',first,4100))

    def test_probe_inventory_errors_are_unknown_not_confirmed_absence(self):
        with patch('forge.routing.subprocess.run', side_effect=subprocess.TimeoutExpired('tmux',5)):
            self.assertIsNone(routing.probe_identity('one-build'))
        with patch('forge.routing.subprocess.run', return_value=subprocess.CompletedProcess([],1,'','permission denied')):
            self.assertIsNone(routing.probe_identity('one-build'))
        with patch('forge.routing.subprocess.run', return_value=subprocess.CompletedProcess([],1,'',"can't find session: one-build")):
            self.assertIs(routing.probe_identity('one-build'),False)

    def test_malformed_routing_evidence_fails_closed(self):
        cases=[None, [], {'providers':[]}, {'last_provider':'other'},
            {'providers':{'claude':{'blocked_until':float('nan')}}},
            {'providers':{'claude':{'probe_generation':'invalid','probe_session':'one-build'}}},
            {'providers':{'claude':{'probe_generation':identity(1),'probe_session':['invalid']}}},
            {'bindings':{'one-build':{'provider':'claude','generation':identity(1),'bound_at':'yesterday'}}}]
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(StateError): routing.validate(value)


class RoutingCliTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.config=self.root/'repos.d';self.config.mkdir()
        (self.config/'one.repo').write_text('REPO=o/one\nOWNER_LABEL=auto:owned-by-fixture\n')
        self.state=self.root/'state';self.state.mkdir()
        binary=self.root/'bin';binary.mkdir()
        for provider in ('claude','codex','grok'):
            path=binary/provider
            path.write_text('#!/bin/sh\nprintf paid > "'+str(self.root/'paid-probe')+'"\nexit 97\n');path.chmod(0o755)
        tmux=binary/'tmux'
        tmux.write_text('#!/usr/bin/python3\nimport os,sys\nprint(os.environ["TEST_GENERATION"])\nsys.exit(int(os.environ.get("TMUX_STATUS","0")))\n');tmux.chmod(0o755)
        self.env=dict(os.environ,PATH=str(binary),PYTHONPATH=str(ROOT),TEST_GENERATION='$1|100|%1|1001',
                      FORGE_CLAUDE_BIN=str(binary/'claude'),FORGE_CODEX_BIN=str(binary/'codex'),FORGE_GROK_BIN=str(binary/'grok'))

    def cli(self,*args):
        result=subprocess.run([sys.executable,'-m','forge.routing','--state',str(self.state),'--config',str(self.config),*args],
                              cwd=ROOT,env=self.env,text=True,capture_output=True,timeout=5)
        self.assertFalse((self.root/'paid-probe').exists(),'Routing executed a paid provider probe')
        return result

    def test_real_cli_select_bind_alternate_without_executing_providers(self):
        selected=self.cli('select','--session','one-build')
        self.assertEqual(selected.returncode,0,selected.stderr);self.assertEqual(selected.stdout.strip(),'claude')
        result=self.cli('bind','--session','one-build','--provider','claude','--generation',self.env['TEST_GENERATION'])
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.cli('select','--session','one-audit').stdout.strip(),'codex')
        result=self.cli('bind','--session','one-audit','--provider','codex','--generation',self.env['TEST_GENERATION'])
        self.assertEqual(result.returncode,0,result.stderr)
        # A configured but unqualified Grok Build is never selected by the legacy supervisor path.
        self.assertEqual(self.cli('select','--session','one-review').stdout.strip(),'claude')
        readiness=json.loads(self.cli('readiness').stdout)
        self.assertEqual((readiness['grok']['status'],readiness['grok']['executable']),('unqualified',str(self.root/'bin/grok')))
        self.assertEqual(readiness['codex']['status'],'ready')
        self.assertEqual(self.cli('current','--session','one-build').stdout.strip(),'claude')
        self.env['TEST_GENERATION']='$2|100|%2|1002'
        self.assertNotEqual(self.cli('current','--session','one-build').returncode,0)
        self.assertNotEqual(self.cli('select','--session','private-dev').returncode,0)

    def test_real_cli_existing_session_gate_is_callable_and_checks_outage(self):
        result=self.cli('allowed','--session','one-build','--provider','claude','--generation',self.env['TEST_GENERATION'])
        self.assertEqual(result.returncode,0,result.stderr)
        result=self.cli('failure','--session','one-build','--provider','claude','--reason','usage_limit','--generation',self.env['TEST_GENERATION'])
        self.assertEqual(result.returncode,0,result.stderr)
        result=self.cli('allowed','--session','one-build','--provider','claude','--generation',self.env['TEST_GENERATION'])
        self.assertNotEqual(result.returncode,0)

    def test_corrupt_and_lost_initialized_state_never_reset_implicitly(self):
        self.assertEqual(self.cli('select','--session','one-build').returncode,0)
        path=self.state/'routing.json';path.unlink()
        self.assertNotEqual(self.cli('select','--session','one-build').returncode,0)
        self.assertFalse(path.exists())
        path.write_text('{broken')
        self.assertNotEqual(self.cli('select','--session','one-build').returncode,0)
        self.assertEqual(path.read_text(),'{broken')

    def test_cli_rejects_stale_failure_identity_without_mutating_receipt(self):
        self.cli('select','--session','one-build')
        before=(self.state/'routing.json').read_bytes()
        result=self.cli('failure','--session','one-build','--provider','claude','--reason','usage_limit','--generation','$9|100|%9|1009')
        self.assertNotEqual(result.returncode,0)
        self.assertEqual((self.state/'routing.json').read_bytes(),before)
