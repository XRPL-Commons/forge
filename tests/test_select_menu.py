import unittest

from forge.navigation import render
from forge.select_menu import SCRIPT, STYLE


class SelectMenuTests(unittest.TestCase):
    def test_every_operator_page_receives_the_shared_select_menu_once(self):
        page = '<!doctype html><html><head><title>t</title></head><body><!--FORGE_NAVIGATION--><select id="a"><option>x</option></select></body></html>'
        for route in ('/', '/issues', '/factory', '/onboarding', '/settings', '/settings/github'):
            rendered = render(page, route)
            self.assertEqual(rendered.count(STYLE), 1, route)
            self.assertEqual(rendered.count(SCRIPT), 1, route)
            self.assertLess(rendered.index(STYLE), rendered.index('</head>'), route)
            self.assertLess(rendered.index('<body'), rendered.index(SCRIPT), route)

    def test_native_select_stays_the_source_of_truth(self):
        self.assertIn("select.dispatchEvent(new Event('change',{bubbles:true}))", SCRIPT)
        self.assertIn('.select-menu__native{position:absolute', STYLE)
        self.assertNotIn('visibility:hidden;', STYLE.split('.select-menu__native')[1].split('}')[0])
        self.assertIn("role','listbox'", SCRIPT)
        self.assertIn("options.length>7", SCRIPT)


if __name__ == '__main__':
    unittest.main()
