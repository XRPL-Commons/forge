"""Offline policy contracts. These do not assert native managed enforcement."""
import json
from pathlib import Path
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1] / 'deploy/executor-policies'

class ExecutorPolicyTests(unittest.TestCase):
    def setUp(self):
        self.requirements = tomllib.loads((ROOT/'codex/requirements.toml').read_text())
        self.config = tomllib.loads((ROOT/'codex/config.toml').read_text())
        self.claude = json.loads((ROOT/'claude/managed-settings.json').read_text())
        self.contract = json.loads((ROOT/'launcher-contract.json').read_text())

    def test_no_activation_or_credential_material_in_contract(self):
        self.assertTrue(self.contract['dummy_only'])
        self.assertIs(self.contract['provider_activation'], False)
        self.assertEqual(self.contract['dummy_credential'], '/home/forge/client/dummy-credential')
        for provider in self.contract['providers'].values():
            self.assertTrue(provider['executable'].startswith('/opt/forge/bin/'))
            self.assertFalse(any('TOKEN' in key or 'API_KEY' in key for key in provider['environment']))

    def test_codex_cannot_select_builtin_or_legacy_escalation(self):
        r = self.requirements
        self.assertEqual(r['allowed_permission_profiles'], {'forge_dummy_executor': True})
        self.assertEqual(r['allowed_approval_policies'], ['never'])
        self.assertEqual(self.config['default_permissions'], r['default_permissions'])
        self.assertNotIn('permissions', self.config) # duplicate managed name is invalid
        self.assertNotIn('sandbox_mode', self.config)
        self.assertNotIn('sandbox_mode', r)

    def test_codex_workspace_fixed_not_caller_expanded(self):
        profile = self.requirements['permissions']['forge_dummy_executor']
        self.assertEqual(profile['extends'], ':read-only')
        fs = profile['filesystem']
        self.assertNotIn(':workspace_roots', fs)
        self.assertEqual([p for p,v in fs.items() if v == 'write'], ['/home/forge/workspace'])
        self.assertEqual(fs[':root'], 'deny')
        self.assertEqual(fs['/opt/forge/bin/codex'], 'read')
        self.assertEqual(fs['/home/forge/client'], 'deny')
        self.assertFalse(profile['network']['enabled'])
        self.assertIn('/home/forge/client', self.requirements['permissions']['filesystem']['deny_read'])
        self.assertEqual(self.config['shell_environment_policy']['inherit'], 'none')

    def test_codex_other_tool_surfaces_denied(self):
        self.assertEqual(self.requirements['mcp_servers'], {})
        self.assertEqual(self.requirements['plugins'], {})
        self.assertEqual(self.requirements['allowed_web_search_modes'], [])
        for key in ('apps','plugins','browser_use','computer_use','multi_agent','hooks','code_mode_host'):
            self.assertIs(self.requirements['features'][key], False)

    def test_claude_no_unsandboxed_retry_or_non_bash_tools(self):
        s=self.claude['sandbox']
        for key in ('enabled','failIfUnavailable'):
            self.assertTrue(s[key])
        for key in ('allowUnsandboxedCommands','enableWeakerNestedSandbox'):
            self.assertFalse(s[key])
        self.assertEqual(s['excludedCommands'], [])
        args=self.contract['providers']['claude']['arguments']
        self.assertEqual(args[args.index('--tools')+1], 'Bash')
        self.assertEqual(args[args.index('--setting-sources')+1], '')
        self.assertIn('--strict-mcp-config',args)
        self.assertIn('--safe-mode',args)
        self.assertEqual(self.claude['allowedMcpServers'], [])
        self.assertEqual(json.loads((ROOT/'claude/managed-mcp.json').read_text()), {'mcpServers':{}})

    def test_claude_client_denial_and_scrub_mandatory(self):
        s=self.claude['sandbox']
        self.assertIn('/home/forge/client',s['filesystem']['denyRead'])
        self.assertTrue(s['filesystem']['allowManagedReadPathsOnly'])
        self.assertEqual(s['network']['allowedDomains'], [])
        self.assertFalse(s['network']['allowAllUnixSockets'])
        self.assertFalse(s['network']['allowLocalBinding'])
        self.assertEqual(self.contract['providers']['claude']['environment']['CLAUDE_CODE_SUBPROCESS_ENV_SCRUB'],'1')
        denied={r['name'] for r in s['credentials']['envVars'] if r['mode']=='deny'}
        self.assertTrue({'ANTHROPIC_API_KEY','CLAUDE_CODE_OAUTH_TOKEN','GH_TOKEN','SSH_AUTH_SOCK'} <= denied)

if __name__ == '__main__': unittest.main()
