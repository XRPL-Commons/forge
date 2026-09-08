"""Candidate-bound evidence checks; models never grant merge authority."""
from dataclasses import asdict, dataclass
import hashlib
import hmac
import json
import re
import shlex
import time


@dataclass(frozen=True)
class Candidate:
    repository: str
    pr: int
    head: str
    base: str
    policy: str

    def __post_init__(self):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", self.repository) or type(self.pr) is not int or self.pr < 1:
            raise ValueError("Invalid repository/PR identity")
        if any(not re.fullmatch(r"[0-9a-f]{40,64}", s) for s in (self.head, self.base)):
            raise ValueError("Exact commit identifiers required")
        if not self.policy:
            raise ValueError("Policy version required")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sign_evidence(candidate, role, provider, outcome, artifacts, key, *, now=None):
    """Trusted control-side operation; signing keys must never enter worker scope."""
    if len(key) < 32:
        raise ValueError("Signing key must contain at least 32 bytes")
    if role not in {"builder", "reviewer", "auditor", "checks"}:
        raise ValueError("Unknown role")
    value = {"schema": 1, "candidate": asdict(candidate), "role": role, "provider": provider,
             "outcome": outcome, "artifacts": artifacts, "created_at": time.time() if now is None else now}
    return {"evidence": value, "signature": hmac.new(key, canonical(value), hashlib.sha256).hexdigest()}


def authorize_merge(candidate, receipts, role_keys, *, labels, owner_labels, expected_owner,
                    healthy_providers, required_checks, now=None, max_age=86400):
    """Return blockers. Caller must reread remote truth before any publication.

    This gate cannot atomically pin a moving base through GitHub's direct merge
    API. Production use additionally requires a protected integration/merge queue.
    """
    now = time.time() if now is None else now
    blockers = []
    if "hold" in labels or "ready-for-audit" not in labels:
        blockers.append("PR is held or not ready for audit")
    if set(labels) & {"ready-for-review", "changes-requested"}:
        blockers.append("Conflicting pipeline labels")
    if set(owner_labels) != {expected_owner}:
        blockers.append("Unknown or conflicting repository ownership")
    verified = {}
    for receipt in receipts:
        try:
            evidence = receipt["evidence"]
            role = evidence["role"]
            key = role_keys[role]
            signature = hmac.new(key, canonical(evidence), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, receipt["signature"]):
                raise ValueError("signature")
            if evidence["schema"] != 1 or evidence["candidate"] != asdict(candidate):
                raise ValueError("candidate")
            if not 0 <= now - evidence["created_at"] <= max_age:
                raise ValueError("age")
            if role in verified or evidence["outcome"] != "pass":
                raise ValueError("duplicate or non-passing verdict")
            if role != "checks" and evidence["provider"] not in {"codex", "claude"}:
                raise ValueError("provider")
            verified[role] = evidence
        except (KeyError, TypeError, ValueError):
            blockers.append("Invalid, stale, duplicate, or non-passing evidence")
    missing = {"builder", "reviewer", "auditor", "checks"} - verified.keys()
    if missing:
        blockers.append("Missing evidence: " + ", ".join(sorted(missing)))
    providers = {v["provider"] for k, v in verified.items() if k != "checks"}
    if {"codex", "claude"} <= set(healthy_providers) and providers != {"codex", "claude"}:
        blockers.append("Both healthy providers must contribute")
    checks = verified.get("checks", {}).get("artifacts", {})
    if not isinstance(checks, dict) or not required_checks or any(checks.get(c) != "pass" for c in required_checks):
        blockers.append("Required trusted checks are missing or failing")
    return blockers


def scan_patch(patch):
    """Deterministic deny rules with diff file/line evidence; never a safety proof."""
    findings = []
    path = None
    line = 0
    rules = (
        ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
        ("token", re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-(?:proj-)?[A-Za-z0-9_-]{20,})")),
        ("download-execute", re.compile(r"\b(?:curl|wget)\b.*\|\s*(?:sudo\s+)?(?:ba|z|da)?sh\b")),
        ("persistence", re.compile(r"(?:/etc/(?:cron|systemd)|\.ssh/authorized_keys|\bcrontab\s|systemctl\s+(?:--user\s+)?enable)")),
        ("encoded-execution", re.compile(r"(?:base64\s+(?:-d|--decode).*\|.*\bsh\b|eval\s*\(.*(?:atob|base64))")),
    )
    old_remaining = new_remaining = 0
    in_hunk = False

    def protected(changed):
        if changed.startswith(".github/workflows/") or "authorized_keys" in changed or changed.startswith(("/etc/cron", "/etc/systemd")):
            findings.append({"file": changed, "line": 1, "rule": "protected-path"})

    for text in patch.splitlines():
        if in_hunk:
            if text.startswith("\\ No newline at end of file"):
                continue
            if text.startswith("+"):
                if path is None or path.startswith('"'):
                    findings.append({"file": path or "unknown", "line": line, "rule": "unsupported-path"})
                for name, expression in rules:
                    if expression.search(text[1:]):
                        findings.append({"file": path, "line": line, "rule": name})
                new_remaining -= 1
                line += 1
            elif text.startswith("-"):
                old_remaining -= 1
            elif text.startswith(" "):
                old_remaining -= 1
                new_remaining -= 1
                line += 1
            else:
                findings.append({"file": path or "unknown", "line": line, "rule": "malformed-hunk"})
                in_hunk = False
            if old_remaining < 0 or new_remaining < 0:
                findings.append({"file": path or "unknown", "line": line, "rule": "malformed-hunk"})
                in_hunk = False
            elif old_remaining == 0 and new_remaining == 0:
                in_hunk = False
            # Hunk payload must never be reinterpreted as a path/header.
            continue
        if text.startswith("diff --git "):
            path = None
            try:
                paths = shlex.split(text)[2:]
                if len(paths) != 2:
                    raise ValueError("unsupported diff header")
                for changed in paths:
                    clean = changed[2:] if changed.startswith(("a/", "b/")) else changed
                    if "\\" in clean:
                        findings.append({"file": clean, "line": 1, "rule": "unsupported-path"})
                    protected(clean)
                path = paths[1][2:]
            except ValueError:
                findings.append({"file": "unknown", "line": 1, "rule": "unsupported-path"})
        elif text.startswith(("rename from ", "rename to ")):
            protected(text.split(" ", 2)[2])
        elif text.startswith("+++ "):
            path = text[4:]
            if path.startswith("b/"):
                path = path[2:]
            if path != "/dev/null":
                protected(path)
        elif text.startswith("--- a/"):
            protected(text[6:])
        elif text.startswith("@@ "):
            match = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", text)
            if not match:
                findings.append({"file": path or "unknown", "line": 1, "rule": "malformed-hunk"})
                continue
            old_remaining = int(match[2] if match[2] is not None else 1)
            line = int(match[3])
            new_remaining = int(match[4] if match[4] is not None else 1)
            in_hunk = old_remaining > 0 or new_remaining > 0
        elif text.startswith(("Binary files ", "GIT binary patch")):
            findings.append({"file": path or "unknown", "line": 1, "rule": "unsupported-binary"})
    if in_hunk:
        findings.append({"file": path or "unknown", "line": line, "rule": "incomplete-hunk"})
    return findings
