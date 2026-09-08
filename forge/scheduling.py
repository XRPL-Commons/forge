"""Deterministic GitHub priority writer and dependency-aware execution plans.

LLM output is a proposal. This controller owns validation and remote mutations.
Existing priority labels are manual unless a recorded controller assignment still
matches and priority:auto remains present. Operator changes latch manual control.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import time
from .config import Repository, load_repositories
from .control import atomic_json, snapshot_path
from .github import GitHub, GitHubError
from .ordering import order_issues

PRIORITIES = {"p0", "p1", "p2", "p3"}
# Bump the relevant version whenever that triage policy changes semantics.
TRIAGE_POLICIES = {"local": "local-v1", "llm": "llm-v1"}


def labels(issue):
    return {x["name"] if isinstance(x, dict) else x for x in issue.get("labels", [])}


class SchedulingGitHub(GitHub):
    def request(self, method, endpoint, data=None):
        args = ["gh", "api", "--method", method, endpoint]
        kwargs = dict(capture_output=True, text=True, timeout=self.timeout)
        if data is not None:
            args += ["--input", "-"]
            kwargs["input"] = json.dumps(data)
        result = self.run(args, **kwargs)
        if result.returncode:
            raise GitHubError(f"GitHub {method} failed (exit {result.returncode})")
        return json.loads(result.stdout) if result.stdout.strip() else None

    def owned(self, config):
        names = {v["name"] for v in self.pages(f"repos/{config.repo}/labels?per_page=100")}
        if not config.enabled or {n for n in names if n.startswith("auto:owned-by-")} != {config.owner_label}:
            raise GitHubError("Repository disabled or ownership uncertain")
        return names

    def items(self, config):
        """Complete open inventory, issues and pull requests, after the ownership check."""
        self.owned(config)
        return self.pages(f"repos/{config.repo}/issues?state=open&per_page=100")

    def issues(self, config):
        return [i for i in self.items(config) if "pull_request" not in i]

    def dependency_map(self, repo, issues, extra=()):
        by_number = {i["number"]: i for i in issues}
        pending = {i["number"] for i in issues if "ready" in labels(i) or "wip" in labels(i)} | set(extra)
        result = {}
        def one(number):
            return number, self.pages(f"repos/{repo}/issues/{number}/dependencies/blocked_by?per_page=100")
        while pending:
            if len(result) + len(pending) > 150:
                raise GitHubError("Dependency closure exceeds admission budget")
            with ThreadPoolExecutor(max_workers=4) as pool:
                batch = dict(pool.map(one, sorted(pending)))
            result.update(batch)
            pending = {d["number"] for values in batch.values() for d in values
                       if d.get("state") == "open" and d.get("repository_url", "").lower() == f"https://api.github.com/repos/{repo}".lower()
                       and d["number"] in by_number and d["number"] not in result}
        return result


def repo_state(state, repo):
    return Path(state) / "scheduling" / (hashlib.sha256(repo.encode()).hexdigest() + ".json")


def fingerprint(issues):
    data = [{"number": i["number"], "revision": issue_revision(i), "state": i.get("state")}
            for i in sorted(issues, key=lambda i: i["number"])]
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def issue_revision(issue):
    return hashlib.sha256(json.dumps({"title": issue.get("title"), "body": issue.get("body"),
        "labels": sorted(labels(issue))}, sort_keys=True).encode()).hexdigest()


def gate_untriaged(repo, issues, plan, state):
    path = repo_state(state, repo)
    ledger = json.loads(path.read_text()) if path.exists() else {"issues": {}}
    pending = []
    for issue in issues:
        if "ready" in labels(issue) and not labels(issue) & {"hold", "wip"}:
            record = ledger.get("issues", {}).get(str(issue["number"]), {})
            if record.get("triaged_revision") != issue_revision(issue):
                pending.append(issue["number"])
                plan["blocked"].setdefault(str(issue["number"]), []).append("triage_pending")
    plan["eligible"] = [n for n in plan["eligible"] if n not in pending]
    plan["triage_pending"] = sorted(pending)
    return plan


def can_set_priority(issue, record):
    current = labels(issue) & PRIORITIES
    if "priority:manual" in labels(issue):
        return False
    if "priority:retriage" in labels(issue):
        return True
    if record.get("manual"):
        return False
    if not current:
        return not record.get("assigned_priority")  # removing an automatic priority is an operator override
    return (len(current) == 1 and "priority:auto" in labels(issue)
            and current == {record.get("assigned_priority")})


def priority_inputs(issues, records):
    output = []
    for issue in issues:
        item = dict(issue)
        if can_set_priority(issue, records.get(str(issue["number"]), {})):
            item["labels"] = [x for x in issue.get("labels", []) if (x.get("name") if isinstance(x, dict) else x) not in PRIORITIES]
        output.append(item)
    return output


def create_plan(repo, issues, dependencies, state, *, handed_off=()):
    path = repo_state(state, repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    # The planner never edits triage ownership state. It has its own lock/file.
    plan_path = path.with_suffix(".plan.json")
    with plan_path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = json.loads(plan_path.read_text()) if plan_path.exists() else {}
        now = time.time()
        since = previous.get("first_ready", {})
        since = {str(i["number"]): since.get(str(i["number"]), now) for i in issues if "ready" in labels(i)}
        plan = {"repo": repo, "observed_at": now, "first_ready": since,
                **order_issues(issues, dependencies, first_ready=since, handed_off=handed_off)}
        gate_untriaged(repo, issues, plan, state)
        visible = {i["number"] for i in issues if labels(i) & {"ready", "wip"}}
        plan["ordered"] = [n for n in plan["ordered"] if n in visible]
        plan["blocked"] = {n: reasons for n, reasons in plan["blocked"].items() if int(n) in visible}
        atomic_json(plan_path, plan)
    return plan


def triage(config, state, *, github=None, model=None, force=False, policy_id=TRIAGE_POLICIES["llm"]):
    from .triage_model import propose, validate
    from .triage_rules import prerequisite_numbers, merge_explicit_dependencies
    if not isinstance(policy_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,79}", policy_id):
        raise ValueError("A versioned triage policy identifier is required")
    github = github or SchedulingGitHub()
    model = model or propose
    path = repo_state(state, config.repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ledger = json.loads(path.read_text()) if path.exists() else {"issues": {}}
        issues = github.issues(config)
        ready = [i for i in issues if "ready" in labels(i) and not labels(i) & {"hold", "wip"}]
        if not ready:
            return {"status": "no_pending_ready_work"}
        if len(ready) > 40:
            raise ValueError("Triage batch exceeds 40 issues; split deliberately rather than truncate")
        by_number = {i["number"]: i for i in issues}
        # Resolve explicit prerequisite references, including closed/cancelled ones and
        # same-repo issue URLs, before a model can accidentally omit them.
        context_numbers = {i["number"] for i in ready}
        pending = set(context_numbers)
        while pending:
            number = min(pending)
            pending.remove(number)
            for target in sorted(prerequisite_numbers(config.repo, by_number[number].get("body") or "")):
                if target in context_numbers:
                    continue
                if len(context_numbers) >= 150:
                    raise GitHubError("Referenced issue context exceeds admission budget")
                if target not in by_number:
                    fetched = github.request("GET", f"repos/{config.repo}/issues/{target}")
                    if (not isinstance(fetched, dict) or fetched.get("number") != target
                            or fetched.get("state") not in ("open", "closed") or "pull_request" in fetched
                            or fetched.get("repository_url", "").lower() != f"https://api.github.com/repos/{config.repo}".lower()):
                        raise GitHubError("Referenced prerequisite context is missing or outside repository")
                    by_number[target] = fetched
                    issues.append(fetched)
                context_numbers.add(target)
                pending.add(target)
        context = [by_number[n] for n in sorted(context_numbers)]
        digest = fingerprint(context)
        now = time.time()
        needs_revision = any(ledger["issues"].get(str(i["number"]), {}).get("triaged_revision") != issue_revision(i)
                             or ledger["issues"].get(str(i["number"]), {}).get("triaged_policy_id") != policy_id for i in ready)
        if not force and ((ledger.get("fingerprint") == digest and ledger.get("policy_id") == policy_id and not needs_revision) or ledger.get("retry_after", 0) > now):
            return {"status": "cached_or_cooldown"}
        # Persist a crash/retry deadline before making a model request.
        ledger["retry_after"] = now + 900
        ledger["attempted_policy_id"] = policy_id
        atomic_json(path, ledger)
        inputs = priority_inputs(context, ledger["issues"])
        proposal = model(config.repo, inputs)
        validate(config.repo, inputs, proposal)
        # Additive native dependencies only. Existing operator dependencies are preserved.
        dependencies = github.dependency_map(config.repo, issues, extra=context_numbers)
        ignored = {}
        for number in context_numbers:
            record = ledger["issues"].get(str(number), {})
            existing = {d["id"] for d in dependencies.get(number, [])}
            ignored[number] = set(record.get("removed_dependencies", [])) | (set(record.get("dependencies", [])) - existing)
        proposal = merge_explicit_dependencies(config.repo, inputs, proposal, ignored_ids_by_number=ignored)
        by_number = {i["number"]: i for i in issues}
        combined = {n: list(values) for n, values in dependencies.items()}
        for recommendation in proposal["issues"]:
            n = recommendation["number"]
            if n not in {i["number"] for i in ready}:
                continue
            for dep in recommendation["dependencies"]:
                combined.setdefault(n, []).append(by_number[dep["number"]])
        test_plan = order_issues(issues, combined)
        if any("cycle" in reason.lower() for reasons in test_plan["blocked"].values() for reason in reasons):
            raise ValueError("Combined dependency proposal introduces a cycle")
        # A model request can be slow: revalidate scope immediately before mutations.
        available = github.owned(config)
        for name, description in (("priority:manual", "Operator-owned priority; Forge must preserve it"),
                                  ("priority:retriage", "Hand this priority back to automatic triage")):
            if name not in available:
                github.request("POST", f"repos/{config.repo}/labels", {"name": name, "color": "1d76db", "description": description})
                available.add(name)
        changes = []
        for recommendation in proposal["issues"]:
            n = recommendation["number"]
            original = by_number[n]
            if n not in {i["number"] for i in ready}:
                continue
            current = github.request("GET", f"repos/{config.repo}/issues/{n}")
            if (current.get("state") != "open" or labels(current) & {"hold", "wip"}
                    or "ready" not in labels(current) or current.get("body") != original.get("body")
                    or current.get("title") != original.get("title") or labels(current) != labels(original)):
                changes.append({"number": n, "status": "changed_during_triage"})
                continue
            record = ledger["issues"].setdefault(str(n), {})
            made = []
            resulting_labels = labels(current)
            if can_set_priority(current, record):
                priority = recommendation["priority"]
                previous = labels(current) & PRIORITIES
                if previous != {priority} or "priority:retriage" in labels(current):
                    for name, color in ((priority, {"p0": "b60205", "p1": "d93f0b", "p2": "fbca04", "p3": "0e8a16"}[priority]), ("priority:auto", "ededed")):
                        if name not in available:
                            github.request("POST", f"repos/{config.repo}/labels", {"name": name, "color": color, "description": "Forge scheduling priority"})
                            available.add(name)
                    # Add first: an interruption leaves conflicting priorities, which block dispatch.
                    github.request("POST", f"repos/{config.repo}/issues/{n}/labels", {"labels": [priority, "priority:auto"]})
                    for old in previous - {priority}:
                        github.request("DELETE", f"repos/{config.repo}/issues/{n}/labels/{old}")
                    if "priority:retriage" in labels(current):
                        github.request("DELETE", f"repos/{config.repo}/issues/{n}/labels/priority%3Aretriage")
                    resulting_labels = (resulting_labels - PRIORITIES - {"priority:retriage"}) | {priority, "priority:auto"}
                    record["manual"] = False
                    record["assigned_priority"] = priority
                    made.append("priority " + priority)
            else:
                record["manual"] = True
            existing = {d["id"] for d in dependencies.get(n, [])}
            removed = set(record.get("dependencies", [])) - existing
            record["removed_dependencies"] = sorted(set(record.get("removed_dependencies", [])) | removed)
            for dep in recommendation["dependencies"]:
                target = by_number[dep["number"]]
                if target["id"] in existing or target["id"] in record["removed_dependencies"]:
                    continue
                github.request("POST", f"repos/{config.repo}/issues/{n}/dependencies/blocked_by", {"issue_id": target["id"]})
                existing.add(target["id"])
                made.append("blocked by #" + str(target["number"]))
            record["dependencies"] = sorted(existing)
            by_number[n] = dict(current, labels=sorted(resulting_labels))
            record["triaged_revision"] = issue_revision(by_number[n])
            record["triaged_policy_id"] = policy_id
            record["reason"] = recommendation["reason"]
            record["observed_at"] = now
            changes.append({"number": n, "changes": made, "reason": recommendation["reason"], "manual_priority": record.get("manual", False)})
            # Persist each remote transition before continuing to another issue.
            atomic_json(path, ledger)
        ledger["fingerprint"] = fingerprint([by_number[i["number"]] for i in context])
        ledger["retry_after"] = 0
        ledger["policy_id"] = policy_id
        ledger["last_result"] = {"status": "applied", "at": now, "changes": changes}
        atomic_json(path, ledger)
        return ledger["last_result"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["triage", "plan", "next"])
    parser.add_argument("--config", default=str(Path.home() / "ops/forge-watchdog/repos.d"))
    parser.add_argument("--state", default=str(Path.home() / "ops/forge-watchdog/state"))
    parser.add_argument("--repo")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--mode", choices=["local", "llm"], default=os.environ.get("FORGE_TRIAGE_MODE", "local"))
    args = parser.parse_args(argv)
    configurations = [c for c in load_repositories(args.config) if c.enabled and (args.repo is None or c.repo == args.repo)]
    if args.repo and not configurations:
        raise SystemExit("Repository is not enabled/configured")
    if args.mode == "llm" and os.environ.get("FORGE_LLM_TRIAGE_ALLOWED") != "1":
        raise SystemExit("LLM triage data/destination authorization is not configured")
    github = SchedulingGitHub()
    next_candidates = []
    for config in configurations:
        try:
            if args.action == "triage":
                if args.mode == "local":
                    from .triage_rules import propose as local_propose
                    value = triage(config, args.state, github=github, model=local_propose, force=args.force, policy_id=TRIAGE_POLICIES["local"])
                else:
                    value = triage(config, args.state, github=github, force=args.force, policy_id=TRIAGE_POLICIES["llm"])
                value["triage_mode"] = args.mode
            else:
                # The builder's `next` must see the same handoff evidence as the
                # supervisor snapshot: a claim whose PR left the builder's stage
                # no longer reserves the builder, so the next issue is admitted.
                from .github import handed_off_claims
                from .pipeline import classify
                items = github.items(config)
                issues = [i for i in items if "pull_request" not in i]
                handed_off = handed_off_claims(items, classify(items, config.author)["work"], config.repo)
                value = create_plan(config.repo, issues, github.dependency_map(config.repo, issues), args.state,
                                    handed_off=handed_off)
            if args.action == "next":
                if value["eligible"]:
                    number = value["eligible"][0]
                    if type(number) is not int or number < 1:
                        raise ValueError("Invalid next issue identity")
                    next_candidates.append(number)
            else:
                print(json.dumps({"repo": config.repo, **value}))
        except Exception as error:
            print(json.dumps({"repo": config.repo, "status": "blocked", "reason": str(error)}),
                  file=sys.stderr if args.action == "next" else sys.stdout)
            if args.repo or args.action == "next":
                return 1
    for number in next_candidates:
        print(number)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
