import copy
import unittest

from forge.ordering import order_issues


def issue(number, *labels, state="open", created="2026-01-01T00:00:00Z"):
    return {"number": number, "id": 1000 + number, "state": state,
            "labels": [{"name": label} for label in labels], "created_at": created,
            "repository_url": "https://api.github.com/repos/luc/relay"}


def dep(number, state="open", reason=None, repo=None):
    result = {"number": number, "state": state, "state_reason": reason}
    if repo:
        result["repository_url"] = "https://api.github.com/repos/" + repo
    return result


class OrderingTests(unittest.TestCase):
    def test_priority_age_id_and_default(self):
        issues = [issue(1, "ready", "p3"), issue(2, "ready"), issue(3, "ready", "p0"),
                  issue(4, "ready", "p1"), issue(5, "ready", "p1")]
        result = order_issues(issues, {}, first_ready={4: "2026-02-01T00:00:00Z", 5: "2026-01-01T00:00:00Z"})
        self.assertEqual(result["eligible"], [3, 5, 4, 2, 1])
        self.assertEqual(result["priorities"]["2"], "p2")
        self.assertEqual(order_issues(list(reversed(issues)), {})["ordered"], [3, 4, 5, 2, 1])

    def test_transitive_inheritance_and_dependency_first_forecast(self):
        issues = [issue(1, "ready", "p1"), issue(2, "ready", "p3"), issue(3, "ready", "p3"), issue(4, "ready", "p2")]
        result = order_issues(issues, {1: [dep(2)], 2: [dep(3)]})
        self.assertEqual(result["ordered"], [3, 2, 1, 4])
        self.assertEqual(result["eligible"], [3, 4])
        self.assertEqual(result["priorities"], {"1": "p1", "2": "p1", "3": "p1", "4": "p2"})
        self.assertIn("blocked_prerequisite:2", result["blocked"]["1"])

    def test_hold_claim_and_conflicting_labels_override_priority(self):
        issues = [issue(1, "ready", "hold", "p0"), issue(2, "ready", "p0", "p1"), issue(3, "p0"), issue(4, "ready")]
        result = order_issues(issues, {})
        self.assertEqual(result["eligible"], [4])
        self.assertIn("hold", result["blocked"]["1"])
        self.assertIn("not_ready", result["blocked"]["3"])
        active = order_issues(issues + [issue(5, "wip", "hold")], {})
        self.assertEqual(active["active"], [5])
        self.assertEqual(active["eligible"], [])
        self.assertIn("builder_reserved_by:5", active["blocked"]["4"])

    def test_closed_dependency_requires_completed_reason(self):
        issues = [issue(1, "ready"), issue(2, "ready"), issue(3, "ready"), issue(4, "ready")]
        result = order_issues(issues, {1: [dep(90, "closed", "completed")],
                                       2: [dep(91, "closed", "not_planned")],
                                       3: [dep(92, "closed")], 4: None})
        self.assertEqual(result["eligible"], [1])
        self.assertIn("dependency_requires_resolution:91:not_planned", result["blocked"]["2"])
        self.assertIn("dependency_snapshot_missing", result["blocked"]["4"])

    def test_missing_open_and_cross_repo_dependencies_never_alias(self):
        issues = [issue(1, "ready", "p0"), issue(2, "ready", "p3"), issue(3, "ready")]
        result = order_issues(issues, {1: [dep(2, repo="other/relay")], 3: [{"number": 99}]})
        self.assertEqual(result["eligible"], [2])
        self.assertEqual(result["priorities"]["2"], "p3")
        self.assertIn("dependency_open:other/relay#2", result["blocked"]["1"])
        self.assertIn("dependency_metadata_missing:99", result["blocked"]["3"])

    def test_cycle_members_and_blocked_descendants(self):
        issues = [issue(i, "ready") for i in range(1, 7)]
        result = order_issues(issues, {1: [dep(2), dep(3)], 2: [dep(1)], 3: [dep(2)], 4: [dep(3)], 5: [dep(4)]})
        self.assertEqual(result["eligible"], [6])
        for member in (1, 2, 3):
            self.assertIn("dependency_cycle", result["blocked"][str(member)])
        self.assertNotIn("dependency_cycle", result["blocked"]["4"])
        self.assertIn("blocked_prerequisite:4", result["blocked"]["5"])
        self_loop = order_issues([issue(1, "ready")], {1: [dep(1)]})
        self.assertIn("dependency_cycle", self_loop["blocked"]["1"])

    def test_contradictory_snapshot_and_closed_claim(self):
        issues = [issue(1, "ready"), issue(2, "ready"), issue(3, "wip", state="closed")]
        result = order_issues(issues, {1: [dep(2, "closed", "completed")]})
        self.assertEqual(result["active"], [])
        self.assertEqual(result["eligible"], [2])
        self.assertIn("dependency_snapshot_conflict:2", result["blocked"]["1"])

    def test_input_immutable_and_stable_with_timezone_offsets(self):
        issues = [issue(2, "ready", created="2026-01-01T01:00:00+01:00"), issue(1, "ready")]
        dependencies = {1: [], 2: []}
        original = copy.deepcopy((issues, dependencies))
        self.assertEqual(order_issues(issues, dependencies)["eligible"], [1, 2])
        self.assertEqual((issues, dependencies), original)

    def test_long_chain_does_not_use_recursive_graph_walk(self):
        issues = [issue(i, "ready") for i in range(1, 1101)]
        dependencies = {i: [dep(i + 1)] for i in range(1, 1100)}
        result = order_issues(issues, dependencies)
        self.assertEqual(result["eligible"], [1100])
        self.assertEqual(result["ordered"], list(range(1100, 0, -1)))

    def test_invalid_snapshot_fails_closed(self):
        with self.assertRaises(ValueError):
            order_issues([issue(1, "ready"), issue(1, "ready")], {})
        with self.assertRaises(ValueError):
            order_issues([issue(1, "ready")], None)

    def test_unsupported_priorities_block_and_repository_scope_is_explicit(self):
        issues = [issue(1, "ready", "p4"), issue(2, "ready", "p5", "p1"),
                  issue(3, "ready", "p01"), issue(4, "ready")]
        result = order_issues(issues, {})
        self.assertEqual(result["eligible"], [4])
        self.assertEqual(result["repository"], "luc/relay")
        for number, label in ((1, "p4"), (2, "p5"), (3, "p01")):
            self.assertIn("unsupported_priority_labels:" + label, result["blocked"][str(number)])
        with self.assertRaises(ValueError):
            order_issues([dict(issue(5, "wip"), state="unknown"), issue(4, "ready")], {})
        foreign = dict(issue(5, "ready"), repository_url="https://api.github.com/repos/other/relay")
        with self.assertRaises(ValueError):
            order_issues([issue(4, "ready"), foreign], {})

    def test_inferred_scope_disambiguates_dependency_of_issue_without_repo_field(self):
        one = issue(1, "ready", "p0")
        two = issue(2, "ready", "p3")
        two.pop("repository_url")
        result = order_issues([one, two], {1: [dep(2, repo="luc/relay")]})
        self.assertEqual(result["eligible"], [2])
        self.assertEqual(result["priorities"]["2"], "p0")
