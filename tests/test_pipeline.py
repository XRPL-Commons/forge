import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace
from forge.config import Repository, load_repositories
from forge.github import GitHub, GitHubError
from forge.pipeline import classify
from forge.control import observe, snapshot_path


def item(n, labels=(), author="me", pr=True, **kw):
    return dict(number=n, labels=[{"name": x} for x in labels], user={"login": author},
                **({"pull_request": {}} if pr else {}), **kw)


class PipelineTests(unittest.TestCase):
    def test_audit_is_never_limbo_and_recovery_is_authored(self):
        data = classify([item(1, ["ready-for-audit"]), item(2), item(3, author="other"),
                         item(4, ["changes-requested"], author="other"), item(5, ["changes-requested"])], "me")
        self.assertEqual(data["work"]["audit"], [1])
        self.assertEqual(data["work"]["limbo"], [2])
        self.assertEqual(data["counts"]["builder"], 2)

    def test_hold_dominates_all_stages(self):
        data = classify([item(i, [stage, "hold"]) for i, stage in enumerate(
            ["ready", "wip", "ready-for-review", "ready-for-audit", "changes-requested"], 1)], "me")
        self.assertEqual(data["counts"]["held"], 5)
        self.assertEqual(sum(v for k, v in data["counts"].items() if k != "held"), 0)

    def test_conflict_and_draft_do_not_launch(self):
        data = classify([item(1, ["ready-for-review", "ready-for-audit"]), item(2, draft=True)], "me")
        self.assertEqual(data["work"]["conflicts"], [1])
        self.assertEqual(data["counts"]["builder"], 0)

    def test_wip_requires_ownership_reconciliation(self):
        data = classify([item(1, ["ready", "wip"], pr=False)], "me")
        self.assertEqual(data["counts"]["wip"], 1)
        self.assertEqual(data["counts"]["builder"], 0)

    def test_handed_off_claim_does_not_reserve_the_builder(self):
        from forge.github import handed_off_claims
        from forge.ordering import order_issues
        items = [item(10, ["wip"], pr=False, state="open"), item(11, ["wip"], pr=False, state="open"),
                 item(12, ["wip"], pr=False, state="open"), item(13, ["ready"], pr=False, state="open"),
                 item(20, ["ready-for-audit", "critical"], body="Closes #10"),
                 item(21, ["ready-for-review"], body="Fixes #11"),
                 item(22, ["changes-requested"], body="Resolves #11"),
                 item(23, ["ready-for-audit"], body="Closes other/repo#12"),
                 item(24, ["ready-for-audit"], body="Closes #99")]
        work = classify(items, "me")["work"]
        # 10: audited, awaiting a human -> handed off. 11: in review but also sent back -> builder must
        # return, keeps the reservation. 12: only a cross-repo link -> no evidence, keeps it.
        self.assertEqual(handed_off_claims(items, work, "o/r"), [10])
        for row in items:
            row.setdefault("repository_url", "https://api.github.com/repos/o/r")
        issues = [i for i in items if "pull_request" not in i]
        self.assertEqual(order_issues(issues, {}, handed_off=[10])["reserving"], [11, 12])
        self.assertEqual(order_issues(issues, {}, handed_off=[10, 11, 12])["eligible"], [13])
        self.assertEqual(handed_off_claims(items, {"wip": [10], "audit": [20]}, "o/r"), [10])
        self.assertEqual(handed_off_claims(items, {"wip": [], "audit": [20]}, "o/r"), [])

    def test_pagination_not_default_thirty(self):
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(returncode=0, stdout=json.dumps([list(range(100)), [100]]))
        self.assertEqual(len(GitHub(run).pages("endpoint")), 101)
        self.assertIn("--paginate", calls[0])

    def test_unknown_does_not_become_empty(self):
        def run(*a, **kw):
            raise subprocess.TimeoutExpired("gh", 1)
        with tempfile.TemporaryDirectory() as state:
            repo = Repository("o/r", "auto:owned-by-me")
            value = observe(repo, state, GitHub(run))
            self.assertEqual(value["status"], "unknown")
            self.assertNotIn("counts", value)
            self.assertEqual(json.loads(snapshot_path(state, repo.repo).read_text()), value)

    def test_missing_owner_stops_before_issue_query(self):
        calls = []
        def run(*a, **kw):
            calls.append(a)
            return SimpleNamespace(returncode=0, stdout="[[]]")
        self.assertEqual(GitHub(run).snapshot(Repository("o/r", "auto:owned-by-me"))["status"], "ownership_blocked")
        self.assertEqual(len(calls), 1)

    def test_snapshot_carries_new_pr_links_and_canonical_revision(self):
        rows = [item(16, ['wip'], pr=False, title='Implement fix', state='open'),
                item(22, ['ready-for-review'], body='Closes #16', title='Fix implementation',
                     html_url='https://github.com/o/r/pull/22', updated_at='2026-09-08T00:00:00Z')]
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            if '/pulls?' in command[-1]:
                value=[[{'number':22,'state':'open','updated_at':'2026-09-08T00:00:00Z',
                    'html_url':'https://github.com/o/r/pull/22','head':{'sha':'a'*40},
                    'base':{'sha':'b'*40,'repo':{'full_name':'o/r'}}}]]
            else:value = [[{'name': 'auto:owned-by-me'}]] if '/labels?' in command[-1] else [rows]
            return SimpleNamespace(returncode=0, stdout=json.dumps(value))
        with patch('forge.scheduling.SchedulingGitHub.dependency_map', return_value={}), \
             patch('forge.ordering.order_issues', return_value={'eligible': [], 'active': [16]}):
            result = GitHub(run).snapshot(Repository('o/r', 'auto:owned-by-me'))
        self.assertEqual(len(calls), 3)
        self.assertEqual(result['item_details']['22']['head_sha'],'a'*40)
        self.assertEqual(result['work']['review'], [22])
        self.assertEqual(result['item_details']['22']['body'], 'Closes #16')
        self.assertEqual(result['item_details']['22']['kind'], 'pr')
        self.assertEqual(result['item_details']['22']['url'], 'https://github.com/o/r/pull/22')
        self.assertEqual(result['item_details']['16']['kind'], 'issue')
        self.assertNotIn('linked_issues', result['item_details']['22'])

    def test_completion_failure_cannot_invalidate_successful_admission_snapshot(self):
        rows=[item(1,['ready'],pr=False,state='open')]
        def run(command,**kwargs):
            value=[[{'name':'auto:owned-by-me'}]] if '/labels?' in command[-1] else [rows]
            return SimpleNamespace(returncode=0,stdout=json.dumps(value))
        with tempfile.TemporaryDirectory() as state, \
             patch('forge.scheduling.SchedulingGitHub.dependency_map',return_value={}), \
             patch('forge.scheduling.create_plan',return_value={'eligible':[1],'active':[]}), \
             patch('forge.completions.refresh',side_effect=RuntimeError('unavailable')):
            result=GitHub(run,state=state).snapshot(Repository('o/r','auto:owned-by-me'))
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['counts']['ready'],1)
        self.assertEqual(result['completion_refresh']['status'],'unavailable')

    def test_config_is_data_and_rejects_shell_statements(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "a.repo"
            p.write_text('REPO="o/r"\nOWNER_LABEL="auto:owned-by-me"\nENABLED=false\n')
            self.assertFalse(load_repositories(directory)[0].enabled)
            p.write_text(p.read_text() + "touch /tmp/forged\n")
            with self.assertRaises(ValueError):
                load_repositories(directory)
