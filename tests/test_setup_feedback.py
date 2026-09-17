import unittest
from unittest.mock import Mock
from forge.onboarding import Onboarding
from forge.setup_help import render
from forge.factory.github import GitHub, GitHubUnavailable


class FeedbackTests(unittest.TestCase):
    def test_progress_identifies_only_the_current_account_or_worker(self):
        data={'operation':{'action':'check','step':'prepare'},'probe_jobs':[]}
        self.assertEqual(Onboarding.activity(data)['target'],'github')
        data['operation']['step']='probe'
        data['probe_jobs']=[{'kind':'provider','fenced':False,'provider_id':'claude-2','runner_config':{'provider':'claude'}}]
        self.assertEqual(Onboarding.activity(data)['model_id'],'claude-2')
        data['probe_jobs'].append({'kind':'reuse'})
        self.assertEqual(Onboarding.activity(data)['target'],'runner')
        data['operation']=None
        self.assertIsNone(Onboarding.activity(data))

    def test_report_lists_only_actual_gaps_and_preserves_the_gate(self):
        github=GitHub(Mock(), {})
        github.connection=lambda role:{'app_id':{'review':2,'audit':3,'controller':4}[role]}
        protection={'required_pull_request_reviews':{'required_approving_review_count':2,'dismiss_stale_reviews':True},'allow_force_pushes':{'enabled':False}}
        github.call=Mock(return_value=protection)
        github.pages=Mock(return_value=[])
        report=github.protection_report({}, {'base_ref':'main'})
        self.assertEqual({r['id'] for r in report['requirements']},{'reviews','stale'})
        self.assertTrue(all(r['passed'] for r in report['requirements']))
        self.assertTrue(github.protections({}, {'base_ref':'main'}))
        github.call.side_effect=GitHubUnavailable('unreadable',status=403)
        report=github.protection_report({}, {'base_ref':'main'})
        self.assertFalse(report['available'])
        self.assertEqual([r['id'] for r in report['requirements']],['readable'])

    def test_help_renders_structure_links_and_escapes_content(self):
        document=render('# Setup\n\n## Repository\n\n1. First\n   continued\n2. Second\n\n- Use `command`\n\n    example <command>\n\n[Install](installation.md) <script>bad()</script> [bad](javascript:alert)')
        for fragment in ('<h1 id="setup">Setup</h1>', '<ol><li>First continued</li><li>Second</li></ol>', '<code>command</code>', '<pre><code>example &lt;command&gt;</code></pre>', 'https://github.com/XRPL-Commons/forge/blob/main/docs/installation.md'):
            self.assertIn(fragment,document)
        self.assertNotIn('<script>',document)
        self.assertNotIn('href="javascript:',document)
