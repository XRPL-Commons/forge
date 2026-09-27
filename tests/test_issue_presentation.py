import unittest

from forge.factory.contracts import claims_issue, clamp
from forge.factory.issues import presentation, reviewers

REPO = {'name': 'example/project', 'enabled': True, 'access_ready': True, 'protection_ready': True}
RUNTIME = {'hold_labels': ['hold', 'blocked'], 'ready_label': 'ready', 'enabled': True}


def issue(number=658, labels=(), **over):
    return {'number': number, 'state': 'open', 'labels': [{'name': name} for name in labels], 'assignees': [], **over}


def failed(count=3):
    return {'state': 'failed', 'reason': 'No verified publication or usable result', 'candidate': None,
            'budget_start': 0, 'failures': [{'charged': True}] * count}


class LinkedWorkTests(unittest.TestCase):
    def test_only_prs_that_claim_the_issue_are_linked(self):
        self.assertTrue(claims_issue('Fixes #658', 'example/project', 658))
        self.assertTrue(claims_issue('This resolves: https://github.com/example/project/issues/658', 'example/project', 658))
        self.assertFalse(claims_issue('Follows the approach discussed in #658', 'example/project', 658))
        self.assertFalse(claims_issue('Fixes #6581', 'example/project', 658))

    def test_forge_drafts_are_earlier_attempts_and_a_failure_is_shown_as_one(self):
        prs = [{'number': 659, 'title': 'partial', 'body': '', 'branch': 'forge/issue-658-daa53604'},
               {'number': 660, 'title': 'partial', 'body': '', 'branch': 'forge/issue-658-89d21b0e'},
               {'number': 664, 'title': 'icons', 'body': 'Related to #658', 'branch': 'issue-639-product-icons'}]
        row = presentation(issue(labels=['ready']), failed(), REPO, RUNTIME, prs)
        self.assertEqual(row['status'], 'attention')
        self.assertEqual(row['actions'], ['retry'])
        self.assertEqual([(p['number'], p['kind']) for p in row['pull_requests']], [(659, 'forge_attempt'), (660, 'forge_attempt')])
        self.assertEqual(row['attempts'], {'failed': 3, 'limit': 3})
        self.assertNotIn('Someone else', row['summary']['headline'])

    def test_someone_elses_claiming_pr_is_still_external_work(self):
        prs = [{'number': 700, 'title': 'mine', 'body': 'Closes #658', 'branch': 'fix-typecheck', 'author': 'someone'}]
        row = presentation(issue(labels=['ready']), {}, REPO, RUNTIME, prs)
        self.assertEqual(row['summary']['headline'], 'Someone else is already working on this')
        self.assertEqual(row['pull_requests'][0]['kind'], 'external')


class HoldAndReviewerTests(unittest.TestCase):
    def test_a_label_hold_says_who_set_it(self):
        events = [{'label': 'hold', 'login': 'lucbocahut', 'at': '2026-09-24T12:49:42Z'},
                  {'label': 'p1', 'login': 'someone', 'at': '2026-09-01T00:00:00Z'}]
        row = presentation(issue(585, labels=['hold'], label_events=events), {'state': 'hold'}, REPO, RUNTIME)
        self.assertEqual(row['held_by'], [events[0]])

    def test_reviewers_combine_collaborators_recent_authors_and_configured_people_once(self):
        entry = {'collaborators': {'rows': [{'login': 'Maintainer', 'name': 'M'}], 'authors': ['maintainer', 'shipper']}}
        self.assertEqual(reviewers(entry, ['lead']), [
            {'login': 'Maintainer', 'name': 'M', 'source': 'collaborator'},
            {'login': 'shipper', 'name': '', 'source': 'recent_author'},
            {'login': 'lead', 'name': '', 'source': 'configured'}])

    def test_long_reasons_are_cut_at_a_word(self):
        text = clamp('a narrowly scoped, time-bounded security exception (residual low esbuild advisory)', 60)
        self.assertTrue(text.endswith('…'))
        self.assertIn(text[:-1].split()[-1], 'a narrowly scoped, time-bounded security exception (residual low esbuild advisory)'.split())


if __name__ == '__main__':
    unittest.main()
