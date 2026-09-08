"""Deterministic, read-only repository builder admission and dependency ordering.

Issue numbers are the IDs in this API. ``ordered`` is a dependency-first forecast
including blocked open issues; ``eligible`` contains only presently runnable work.
Effective priorities inherit urgency from open dependents. ``blocked`` explains
each refusal, independently of the model or GitHub label transition machinery.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import heapq
import math
import re


def _labels(issue):
    labels = issue.get("labels", [])
    if not isinstance(labels, list):
        raise ValueError("issue labels must be a complete list")
    result = set()
    for label in labels:
        name = label.get("name") if isinstance(label, dict) else label
        if not isinstance(name, str):
            raise ValueError("invalid issue label")
        result.add(name.lower())
    return result


def _repo(value):
    repository = value.get("repository_url") or value.get("repo")
    if isinstance(repository, dict):
        repository = repository.get("full_name")
    if not isinstance(repository, str):
        return None
    return repository.removeprefix("https://api.github.com/repos/").strip("/").lower()


def _timestamp(value):
    if type(value) in (int, float):
        return float(value) if math.isfinite(value) else float("inf")
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except (ValueError, OverflowError):
            pass
    return float("inf")


def _cycle_members(graph):
    """Iterative strongly connected components; no recursion-depth dependency."""
    visited, finished = set(), []
    for start in sorted(graph):
        if start in visited:
            continue
        visited.add(start)
        stack = [(start, iter(sorted(graph[start])))]
        while stack:
            node, children = stack[-1]
            child = next(children, None)
            if child is None:
                finished.append(node)
                stack.pop()
            elif child not in visited:
                visited.add(child)
                stack.append((child, iter(sorted(graph[child]))))
    reverse = {node: set() for node in graph}
    for node, dependencies in graph.items():
        for dependency in dependencies:
            reverse[dependency].add(node)
    visited, cycles = set(), set()
    for start in reversed(finished):
        if start in visited:
            continue
        members, stack = [], [start]
        visited.add(start)
        while stack:
            node = stack.pop()
            members.append(node)
            for child in reverse[node]:
                if child not in visited:
                    visited.add(child)
                    stack.append(child)
        if len(members) > 1 or start in graph[start]:
            cycles.update(members)
    return cycles


def order_issues(issues, dependencies, *, first_ready=None):
    """Return a stable forecast, admitted numbers, blockers, claims and priorities.

    ``dependencies`` maps issue numbers to complete lists of REST dependency
    objects. An absent mapping entry means no dependencies; an explicit None or
    malformed list blocks admission. Cross-repository dependencies never alias a
    local issue with the same number. Closed dependencies are satisfied only when
    GitHub reports ``state_reason=completed``. A wip claim reserves this repository's
    single builder even when that claim also carries hold or is otherwise blocked.

    Pure function: neither input is modified and no clock/network/state is read.
    """
    if not isinstance(dependencies, dict):
        raise ValueError("dependency snapshot must be a mapping")
    first_ready = first_ready or {}
    rows, labels = {}, {}
    for issue in issues:
        if not isinstance(issue, dict):
            raise ValueError("invalid issue object")
        number = issue.get("number")
        if type(number) is not int or number <= 0 or number in rows:
            raise ValueError("unique positive issue numbers required")
        if not isinstance(issue.get("state"), str) or issue["state"].lower() not in {"open", "closed"}:
            raise ValueError("issue state is unknown; refuse builder admission")
        rows[number] = issue
        labels[number] = _labels(issue)
    scopes = {_repo(row) for row in rows.values() if _repo(row)}
    if len(scopes) > 1:
        raise ValueError("order one repository snapshot at a time")
    repository = next(iter(scopes), None)
    open_rows = {n: row for n, row in rows.items() if row.get("state", "").lower() == "open"}
    graph = {n: set() for n in open_rows}
    blocked = {n: set() for n in open_rows}
    priority = {}
    for number, row in open_rows.items():
        names = labels[number]
        priorities = sorted(int(name[1:]) for name in names if re.fullmatch(r"p[0-3]", name))
        priority[number] = min(priorities, default=2)
        unsupported = sorted(name for name in names if re.fullmatch(r"p[0-9]+", name)
                             and name not in {"p0", "p1", "p2", "p3"})
        if unsupported:
            blocked[number].add("unsupported_priority_labels:" + ",".join(unsupported))
        if len(priorities) > 1:
            blocked[number].add("conflicting_priority_labels:" + ",".join("p" + str(p) for p in priorities))
        if "hold" in names:
            blocked[number].add("hold")
        if "wip" in names:
            blocked[number].add("active_claim")
        if "ready" not in names:
            blocked[number].add("not_ready")
        items = dependencies.get(number, dependencies.get(str(number), []))
        if not isinstance(items, list):
            blocked[number].add("dependency_snapshot_missing")
            continue
        for dependency in items:
            if not isinstance(dependency, dict) or type(dependency.get("number")) is not int:
                blocked[number].add("dependency_metadata_missing")
                continue
            dep_number = dependency["number"]
            dep_repo = _repo(dependency)
            identity = f"{dep_repo}#{dep_number}" if dep_repo else str(dep_number)
            local = rows.get(dep_number)
            # Explicit repository/global-ID disagreement always wins over number.
            if local is not None:
                local_repo = _repo(local) or _repo(row) or repository
                if dep_repo and (not local_repo or dep_repo != local_repo):
                    local = None
                elif dependency.get("id") is not None and local.get("id") is not None and dependency["id"] != local["id"]:
                    local = None
            state = dependency.get("state")
            reason = dependency.get("state_reason")
            if not isinstance(state, str):
                blocked[number].add("dependency_metadata_missing:" + identity)
                continue
            state = state.lower()
            if state == "closed" and reason == "completed":
                # Contradictory local snapshot is unknown, never silently satisfied.
                if local is not None and local.get("state", "").lower() == "open":
                    blocked[number].add("dependency_snapshot_conflict:" + identity)
                continue
            if state == "closed":
                blocked[number].add("dependency_requires_resolution:" + identity + ":" + str(reason or "unknown"))
                continue
            if state != "open":
                blocked[number].add("dependency_metadata_missing:" + identity)
                continue
            blocked[number].add("dependency_open:" + identity)
            if local is not None and dep_number in open_rows:
                graph[number].add(dep_number)
            elif local is not None:
                blocked[number].add("dependency_snapshot_conflict:" + identity)

    # A dependent's urgency flows toward every unresolved prerequisite, including
    # prerequisites that are held/non-ready; it does not override those blockers.
    queue = deque(sorted(open_rows))
    while queue:
        node = queue.popleft()
        for prerequisite in graph[node]:
            if priority[prerequisite] > priority[node]:
                priority[prerequisite] = priority[node]
                queue.append(prerequisite)
    for node in _cycle_members(graph):
        blocked[node].add("dependency_cycle")
    # Explain downstream parked work without copying an unbounded list of every
    # ancestor's messages. Direct references preserve the chain for the dashboard.
    for node, prerequisites in graph.items():
        for prerequisite in prerequisites:
            if blocked[prerequisite]:
                blocked[node].add("blocked_prerequisite:" + str(prerequisite))

    def key(number):
        ready_at = first_ready.get(number, first_ready.get(str(number)))
        return (priority[number], _timestamp(ready_at if ready_at is not None else open_rows[number].get("created_at")), number)

    reverse = {node: set() for node in graph}
    remaining = {node: len(prerequisites) for node, prerequisites in graph.items()}
    for node, prerequisites in graph.items():
        for prerequisite in prerequisites:
            reverse[prerequisite].add(node)
    ready = [key(node) for node in graph if not remaining[node]]
    heapq.heapify(ready)
    ordered = []
    while ready:
        _, _, node = heapq.heappop(ready)
        ordered.append(node)
        for dependent in reverse[node]:
            remaining[dependent] -= 1
            if remaining[dependent] == 0:
                heapq.heappush(ready, key(dependent))
    placed = set(ordered)
    ordered.extend(sorted((node for node in graph if node not in placed), key=key))
    active = sorted(n for n in open_rows if "wip" in labels[n])
    candidates = [number for number in ordered if not blocked[number]]
    if active:
        reason = "builder_reserved_by:" + ",".join(map(str, active))
        for number in candidates:
            blocked[number].add(reason)
    return {"repository": repository, "ordered": ordered, "eligible": [] if active else candidates,
            "blocked": {str(n): sorted(blocked[n]) for n in sorted(blocked) if blocked[n]},
            "active": active, "priorities": {str(n): "p" + str(priority[n]) for n in sorted(priority)}}
