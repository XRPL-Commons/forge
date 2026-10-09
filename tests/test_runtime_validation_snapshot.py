"""Nested config validation consumes one current installation manifest snapshot."""
import unittest
from unittest.mock import patch

from forge.factory.config import validate


class RuntimeValidationSnapshotTests(unittest.TestCase):
    def test_nested_provider_tier_and_discussion_validation_share_one_catalog(self):
        value = {'provider_pool':[
            {'id':'coding','provider':'codex','model':None,'provider_environment':['OPENAI_API_KEY'],'provider_auth_file_env':None},
            {'id':'other','provider':'claude','model':None,'provider_environment':[],'provider_auth_file_env':'CLAUDE_FILE'}],
            'discussion_model':{'codex':'quick','claude':'quick'},
            'environment_tiers':{'standard':{'image':'sha256:'+'a'*64,'memory':'4g','cpus':2}}}
        with patch('forge.factory.config.manifests', return_value={'codex':{},'claude':{}}) as catalog:
            config = validate(value)
            catalog.assert_called_once_with()
            self.assertEqual(config['provider_pool'], value['provider_pool'])
            self.assertEqual(config['environment_tiers'], value['environment_tiers'])
            # The next operation reads the installed contract anew.
            catalog.return_value = {'codex':{}}
            with self.assertRaisesRegex(ValueError, 'installed providers'):
                validate(value)
            self.assertEqual(catalog.call_count, 2)

    def test_invalid_nested_provider_still_rejects_the_configuration(self):
        with patch('forge.factory.config.manifests', return_value={'codex':{}}):
            with self.assertRaisesRegex(ValueError, 'installed harness'):
                validate({'provider_pool':[{'id':'missing','provider':'absent','model':None,
                    'provider_environment':[],'provider_auth_file_env':None}]})
