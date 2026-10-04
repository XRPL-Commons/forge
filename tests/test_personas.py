"""The server's system lines and the UI name the same persona for each provider."""
from pathlib import Path
import re
import unittest

from forge.factory.personas import PERSONAS, persona

UI = Path(__file__).resolve().parents[1] / 'ui/app/utils/personas.ts'


class PersonasTest(unittest.TestCase):
    def test_ui_and_server_agree(self):
        found = {provider: (name, label) for provider, name, label in
                 re.findall(r"(\w+): \{ name: '([^']+)', providerLabel: '([^']+)'", UI.read_text())}
        self.assertEqual(found, {p: (v['name'], v['provider_label']) for p, v in PERSONAS.items()})

    def test_stable_with_fallback(self):
        self.assertEqual(persona('claude')['name'], 'Ember')
        self.assertEqual(persona('codex')['name'], 'Bolt')
        self.assertEqual(persona('grok')['name'], 'Nova')
        self.assertEqual(persona('mistral')['name'], 'Mistral')
        self.assertEqual(persona(None)['name'], 'Agent')


if __name__ == '__main__': unittest.main()
