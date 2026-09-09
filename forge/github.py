"""Bounded, paginated GitHub reads. Errors never mean an empty queue."""
import json
import re
from datetime import datetime
import subprocess
import time


class GitHubError(RuntimeError):
    pass


def handed_off_claims(items, work, repo):
    """Wip claims whose open PR has already left the builder's stage.

    A claim is handed off when an open PR in this repository closes it and that
    PR sits at ready-for-review or ready-for-audit (including a CRITICAL PR
    awaiting a human). The builder was released for it, so the claim must not
    reserve the builder. Any sent-back, unlabeled (limbo) or conflicting PR for
    the same claim keeps the reservation: the builder has to return to it.
    Links come from the same inventory bodies the snapshot already holds; no
    extra GitHub read and no inferred link.
    """
    from .issue_flow import _references
    by_number = {item.get("number"): item for item in items if "pull_request" in item}
    wip = {n for n in work.get("wip", []) if type(n) is int}
    past_builder, needs_builder = set(), set()
    for stage, target in (("review", past_builder), ("audit", past_builder),
                          ("sentback", needs_builder), ("limbo", needs_builder), ("conflicts", needs_builder)):
        for number in work.get(stage, []):
            pr = by_number.get(number)
            if pr is None:
                continue
            target.update(n for n in _references({"body": pr.get("body") or ""}, repo) if n in wip)
    return sorted(past_builder - needs_builder)


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

    def pr_revisions(self, repository, details, previous):
        """Join a canonical pull inventory to this issue observation, never by age alone."""
        prs = {n: d for n, d in details.items() if d['kind'] == 'pr'}
        if not prs:
            return
        old = (previous.get('item_details', {}) if previous.get('status') == 'ok'
               and str(previous.get('repo', '')).lower() == repository.repo.lower() else {})
        if not isinstance(old, dict):
            old = {}
        pending = {}
        for number, detail in prs.items():
            saved = old.get(number, {})
            if not isinstance(saved, dict):
                saved = {}
            stamp = detail.get('updated_at')
            try:
                parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                valid_stamp = parsed.tzinfo is not None
            except (AttributeError, TypeError, ValueError):
                valid_stamp = False
            if (valid_stamp and saved.get('kind') == 'pr'
                    and saved.get('revision_status') == 'canonical'
                    and saved.get('revision_updated_at') == stamp == saved.get('updated_at')
                    and all(isinstance(saved.get(k), str) and re.fullmatch('[0-9a-f]{40,64}', saved[k])
                            for k in ('head_sha', 'base_sha'))):
                detail.update({k: saved[k] for k in ('head_sha', 'base_sha', 'revision_status', 'revision_updated_at')})
            else:
                detail.update(head_sha=None, base_sha=None, revision_status='unknown', revision_updated_at=None)
                if valid_stamp:
                    pending[number] = detail
        if not pending:
            return
        pulls = self.pages(f"repos/{repository.repo}/pulls?state=open&per_page=100")
        if len(pulls) > 10000:
            raise GitHubError('PR revision inventory unbounded')
        by_number = {}
        for pr in pulls:
            if not isinstance(pr, dict) or type(pr.get('number')) is not int:
                raise GitHubError('PR revision inventory malformed')
            key = str(pr['number'])
            if key in by_number:
                raise GitHubError('PR revision inventory duplicated')
            by_number[key] = pr
        for number, detail in pending.items():
            pr = by_number.get(number, {})
            base, head = pr.get('base') or {}, pr.get('head') or {}
            if not isinstance(base, dict) or not isinstance(head, dict) or not isinstance(base.get('repo'), dict):
                continue
            stamp = detail.get('updated_at')
            if (not isinstance(stamp, str) or not stamp or pr.get('updated_at') != stamp
                    or pr.get('state') != 'open'
                    or str(pr.get('html_url', '')).lower() != f'https://github.com/{repository.repo}/pull/{number}'.lower()
                    or str((base.get('repo') or {}).get('full_name', '')).lower() != repository.repo.lower()
                    or any(not isinstance(ref.get('sha'), str) or not re.fullmatch('[0-9a-f]{40,64}', ref['sha'])
                           for ref in (head, base))):
                continue
            detail.update(head_sha=head['sha'], base_sha=base['sha'],
                          revision_status='canonical', revision_updated_at=stamp)

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
        handed_off = handed_off_claims(items, result["work"], repository.repo)
        dependencies = SchedulingGitHub(run=self.run, timeout=self.timeout).dependency_map(repository.repo, issues)
        schedule = (create_plan(repository.repo, issues, dependencies, self.state, handed_off=handed_off) if self.state
                    else order_issues(issues, dependencies, handed_off=handed_off))
        result["counts"]["ready_total"] = result["counts"]["ready"]
        result["counts"]["ready"] = len(schedule["eligible"])
        result["counts"]["wip"] = len(schedule["active"])
        result["counts"]["builder"] = len(schedule["eligible"]) + result["counts"]["sentback"] + result["counts"]["limbo"]
        result["schedule"] = schedule
        # Carry identity and explicit closing text in the same observation as
        # queue numbers. The slower history collector must not gate attribution
        # of a newly opened PR. Explicit links come from this inventory; canonical
        # revision enrichment below adds at most one paginated pull inventory.
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
        previous = {}
        if self.state:
            try:
                from .control import snapshot_path
                path = snapshot_path(self.state, repository.repo)
                previous = json.loads(path.read_text()) if path.exists() else {}
                if not isinstance(previous, dict):
                    previous = {}
            except (OSError, ValueError):
                previous = {}
        self.pr_revisions(repository, result['item_details'], previous)
        if self.state:
            # Best-effort lifecycle enrichment cannot turn a complete open
            # inventory into unknown admission or block on history collection.
            try:
                from .completions import refresh
                result['completion_refresh'] = refresh(self.state, repository.repo, items, previous, run=self.run)
            except Exception:
                result['completion_refresh'] = {'status': 'unavailable'}
        return {"repo": repository.repo, "enabled": True, "status": "ok", "observed_at": time.time(), **result}
