import json, os, subprocess, hashlib
from pathlib import Path

def format_alert(repo, sha, author, subject, verdict):
    url = f"https://github.com/{repo}/commit/{sha}"
    anchors = ", ".join(f"{f['file']}:{f['line']} ({f['side']})" for f in verdict.findings)
    citations = f" Findings: {anchors}." if anchors else ""
    return (f"[observer] {verdict.level.upper()} on {repo}@{sha[:8]} "
            f"by {author}: {verdict.reason} "
            f"({verdict.confidence}) — \"{subject}\" {url}{citations}")

def append_finding(finding, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(finding, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())

def _default_runner(cmd):
    return subprocess.run(cmd, timeout=90).returncode == 0

def notify(message, notify_sh, runner=_default_runner):
    return runner([notify_sh, message])

def report(repo, sha, author, subject, verdict, state_dir, notify_sh,
           runner=_default_runner):
    if verdict.level not in ("suspicious", "malicious", "unknown"):
        return
    alert = format_alert(repo, sha, author, subject, verdict)
    key = hashlib.sha256(alert.encode()).hexdigest()
    marker = Path(state_dir) / ("reported-" + key)
    if marker.exists():
        return
    append_finding({
        "repo": repo, "sha": sha, "author": author, "subject": subject,
        "level": verdict.level, "reason": verdict.reason,
        "confidence": verdict.confidence, "alert": alert,
        "complete": verdict.complete, "findings": verdict.findings,
    }, os.path.join(state_dir, "findings.jsonl"))
    if not runner([notify_sh, "--event-key", "observer-" + key, alert]):
        raise RuntimeError("finding persisted but notification enqueue failed")
    from forge.notifications import atomic_json
    atomic_json(marker, {"enqueued": True, "key": key})
