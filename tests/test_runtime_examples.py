import json
from pathlib import Path
import re
import unittest

from forge.factory.config import DEFAULT, validate

ROOT = Path(__file__).resolve().parent.parent


class RuntimeExampleTests(unittest.TestCase):
    """The examples and docs restate forge/factory/config.py; they must not drift from it."""

    def example(self, name):
        return json.loads((ROOT / 'examples' / name).read_text())

    def test_runtime_example_is_the_complete_default(self):
        example = self.example('runtime.json.example')
        validate(example)
        self.assertEqual(set(example), set(DEFAULT))
        for key, value in DEFAULT.items():
            if key not in ('provider_pool',):
                self.assertEqual(example[key], value, key)

    def test_host_runtime_example_only_overlays_defaults(self):
        example = self.example('host-runtime.json.example')
        validate(example)
        self.assertLessEqual(set(example), set(DEFAULT))
        for key, value in example.items():
            if key != 'image':
                self.assertEqual(value, DEFAULT[key], key)

    def test_docs_state_the_default_slots(self):
        slots = DEFAULT['slots']
        wording = (f"{slots['build']} Build, {slots['review']} Review, {slots['audit']} Audit "
                   f"and {slots['recovery']} Recovery")
        for doc in ('docs/factory-runtime.md', 'docs/public-defaults.md'):
            text = re.sub(r'\s+', ' ', (ROOT / doc).read_text())
            self.assertIn(wording, text, doc)

    def test_install_docs_name_every_shipped_harness(self):
        from forge.harnesses.registry import manifests
        for doc in ('docs/installation.md', 'docs/onboarding.md'):
            text = (ROOT / doc).read_text()
            for item in manifests().values():
                self.assertIn(item['name'], text, doc)


class DiscussionModelTests(unittest.TestCase):
    def test_discussion_model_defaults_to_the_provider_default(self):
        self.assertIsNone(DEFAULT['discussion_model'])
        self.assertIsNone(validate({})['discussion_model'])

    def test_discussion_model_maps_installed_providers_to_models(self):
        self.assertEqual(validate({'discussion_model': {'claude': 'claude-haiku-4-5'}})['discussion_model'],
                         {'claude': 'claude-haiku-4-5'})
        for bad in ('claude-haiku-4-5', {'nobody': 'fast'}, {'claude': ''}, {'claude': None},
                    {'claude': 'two words'}, ['claude'], {'claude': 'x' * 200}):
            with self.assertRaisesRegex(ValueError, 'discussion_model'):
                validate({'discussion_model': bad})


if __name__ == '__main__':
    unittest.main()
