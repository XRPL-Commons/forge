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
        self.assertEqual(row['summary']['headline'], '@someone has pull request #700 open')
        self.assertEqual(row['pull_requests'][0]['kind'], 'external')


class OwnershipTests(unittest.TestCase):
    def test_an_assigned_issue_names_the_assignee_and_how_to_hand_it_over(self):
        row = presentation(issue(612, labels=['ready'], assignees=['lucbocahut']), {}, REPO, RUNTIME)
        self.assertEqual(row['summary']['headline'], 'Assigned to @lucbocahut')
        self.assertIn('Unassign it', row['summary']['plain'])

    def test_a_wip_label_says_so(self):
        row = presentation(issue(639, labels=['wip']), {}, REPO, RUNTIME)
        self.assertEqual(row['summary']['headline'], 'Marked work in progress')


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


class OwnDraftTests(unittest.TestCase):
    def test_own_draft_is_forges_newest_open_pr_for_the_issue(self):
        from unittest.mock import Mock
        from forge.factory.github import GitHub
        github = object.__new__(GitHub)
        pr = lambda n, ref, user, updated, repo=7: {'number': n, 'head': {'ref': ref, 'repo': {'id': repo}},
            'base': {'repo': {'id': 7}}, 'state': 'open', 'user': {'login': user, 'type': 'Bot'}, 'updated_at': updated}
        github.pulls = Mock(return_value=[
            pr(659, 'forge/issue-658-daa53604', 'forge-build[bot]', '2026-09-25T21:00:00Z'),
            pr(660, 'forge/issue-658-89d21b0e', 'forge-build[bot]', '2026-09-25T23:00:00Z'),
            pr(661, 'forge/issue-658-fork', 'forge-build[bot]', '2026-09-26T00:00:00Z', repo=99),
            pr(664, 'issue-639-product-icons', 'someone', '2026-09-27T00:00:00Z'),
            pr(700, 'forge/issue-6580-x', 'forge-build[bot]', '2026-09-27T00:00:00Z')])
        item = {'repo': 'example/project', 'repo_id': 7}
        self.assertEqual(github.own_draft(item, 658, 'forge-build[bot]'),
                         {'pr': 660, 'branch': 'forge/issue-658-89d21b0e', 'superseded': [659]})
        self.assertIsNone(github.own_draft(item, 658, 'other[bot]'))
        github.pulls.return_value = [pr(660, 'forge/issue-658-x', 'forge-build[bot]', '2026-09-25T23:00:00Z')]
        self.assertEqual(github.own_draft(item, 658, 'forge-build[bot]')['superseded'], [])
        for field, value in [('state', 'closed'), ('user', {'login': 'forge-build[bot]', 'type': 'User'}),
                             ('base', {'repo': {'id': 99}})]:
            original = github.pulls.return_value[0]
            github.pulls.return_value = [{**original, field: value}]
            self.assertIsNone(github.own_draft(item, 658, 'forge-build[bot]'))
            github.pulls.return_value = [original]
