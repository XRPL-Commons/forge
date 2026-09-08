"""Bounded, paginated GitHub reads. Errors never mean an empty queue."""
import json
import subprocess
import time


class GitHubError(RuntimeError):
    pass


class GitHub:
    def __init__(self, run=subprocess.run, timeout=45, state=None):
        self.run = run
        self.timeout = timeout
        self.state = state

    def pages(self, endpoint):
        try:
            p = self.run(["gh", "api", "--paginate", "--slurp", endpoint], capture_output=True,
                         text=True, timeout=self.timeout)
            if p.returncode:
                raise GitHubError(f"GitHub read failed (exit {p.returncode})")
            pages = json.loads(p.stdout)
            if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
                raise ValueError("Expected array pages")
            return [item for page in pages for item in page]
        except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
            raise GitHubError(f"GitHub state unavailable: {type(exc).__name__}") from exc

    def snapshot(self, repository):
        if not repository.enabled:
            return {"repo": repository.repo, "enabled": False, "status": "disabled", "observed_at": time.time()}
        labels = self.pages(f"repos/{repository.repo}/labels?per_page=100")
        names = {label["name"] for label in labels}
        others = sorted(x for x in names if x.startswith("auto:owned-by-") and x != repository.owner_label)
        if others or repository.owner_label not in names:
            return {"repo": repository.repo, "enabled": True, "status": "ownership_blocked",
                    "observed_at": time.time(), "reason": "conflicting or missing owner label"}
        items = self.pages(f"repos/{repository.repo}/issues?state=open&per_page=100")
        from .pipeline import classify
        result = classify(items, repository.author)
        from .scheduling import SchedulingGitHub, create_plan
        from .ordering import order_issues
        issues = [i for i in items if "pull_request" not in i]
        dependencies = SchedulingGitHub(run=self.run, timeout=self.timeout).dependency_map(repository.repo, issues)
        schedule = (create_plan(repository.repo, issues, dependencies, self.state) if self.state
                    else order_issues(issues, dependencies))
        result["counts"]["ready_total"] = result["counts"]["ready"]
        result["counts"]["ready"] = len(schedule["eligible"])
        result["counts"]["wip"] = len(schedule["active"])
        result["counts"]["builder"] = len(schedule["eligible"]) + result["counts"]["sentback"] + result["counts"]["limbo"]
        result["schedule"] = schedule
        # Carry identity and explicit closing text in the same observation as
        # queue numbers. The slower history collector must not gate attribution
        # of a newly opened PR. No extra GitHub request or inferred claim link.
        referenced = {number for values in result['work'].values() for number in values}
        result['item_details'] = {
            str(item['number']): {
                'number': item['number'], 'kind': 'pr' if 'pull_request' in item else 'issue',
                'title': item.get('title'), 'body': item.get('body') or '',
                'url': item.get('html_url'), 'state': item.get('state', 'open'),
                'created_at': item.get('created_at'), 'updated_at': item.get('updated_at'),
                'labels': [label['name'] if isinstance(label, dict) else label for label in item['labels']],
                'draft': item.get('draft', False),
            } for item in items if item['number'] in referenced}
        return {"repo": repository.repo, "enabled": True, "status": "ok", "observed_at": time.time(), **result}
