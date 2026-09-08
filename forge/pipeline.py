"""Shared eligibility used by the controller, legacy bridge, and dashboard."""
STAGES = frozenset({"ready-for-review", "ready-for-audit", "changes-requested"})


def classify(items, author):
    work = {k: [] for k in ("ready", "wip", "review", "audit", "sentback", "limbo", "held", "conflicts")}
    stale = []
    for item in items:
        labels = {x["name"] if isinstance(x, dict) else x for x in item["labels"]}
        number = item["number"]
        if "hold" in labels:
            work["held"].append(number)
            continue
        if "pull_request" not in item:
            if "wip" in labels:
                work["wip"].append(number)
                stale.append({"number": number, "updated_at": item.get("updated_at")})
            elif "ready" in labels:
                work["ready"].append(number)
            continue
        stages = labels & STAGES
        if len(stages) > 1:
            work["conflicts"].append(number)
        elif "ready-for-review" in stages:
            work["review"].append(number)
        elif "ready-for-audit" in stages:
            work["audit"].append(number)
        elif item["user"]["login"] == author:
            if "changes-requested" in stages:
                work["sentback"].append(number)
            elif not item.get("draft", False):
                work["limbo"].append(number)
    counts = {key: len(values) for key, values in work.items()}
    counts["builder"] = counts["ready"] + counts["sentback"] + counts["limbo"]
    # wip alone is not proof of an abandoned owned claim. Recovery needs a ledger.
    return {"counts": counts, "work": work, "stale_candidates": stale}
