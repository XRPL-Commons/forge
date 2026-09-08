"""Contain legacy terminal watchdog retries until structured workers replace it.

Terminal errors are only a compatibility signal, never proof of progress. A
quiet worker is parked after bounded nudges rather than killed and duplicated.
"""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from .control import atomic_json


ERRORS = (
    ("blocked_auth", re.compile(r"(?:not logged in|please (?:run /login|log in)|authentication (?:failed|error)|invalid api key|oauth token.*expired)", re.I)),
    ("blocked_quota", re.compile(r"(?:credit balance is too low|out of (?:extra usage|credits)|usage limit reached|you.ve hit your limit|insufficient_quota)", re.I)),
    ("blocked_permission", re.compile(r"(?:sandbox.*(?:failed|operation not permitted)|permission denied)", re.I)),
)


def classify(text):
    for kind, expression in ERRORS:
        if expression.search(text):
            return kind
    return None


class LegacyGuard:
    def __init__(self, directory):
        self.directory = Path(directory) / "legacy-health"
        self.directory.mkdir(parents=True, exist_ok=True)

    def transact(self, action, session, text="", now=None):
        now = time.time() if now is None else now
        path = self.directory / "guard.json"
        with (self.directory / "guard.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                state = json.loads(path.read_text()) if path.exists() else {"sessions": {}, "account_until": 0}
            except (ValueError, OSError):
                return False, "state_corrupt"
            record = state["sessions"].setdefault(session, {"until": 0, "launches": []})
            kind = classify(text) if action == "observe" else None
            if kind:
                record["reason"] = kind
                record["until"] = now + 3600
                if kind in {"blocked_auth", "blocked_quota"}:
                    state["account_until"] = now + 3600
                    state["account_reason"] = kind
            if action == "park":
                record["reason"] = "no_progress"
                record["until"] = now + 3600
            allowed = max(record["until"], state["account_until"]) <= now
            reason = state.get("account_reason", "cooldown") if state["account_until"] > now else record.get("reason", "healthy")
            if action == "launch" and allowed:
                record["launches"] = [t for t in record["launches"] if t > now - 3600]
                if len(record["launches"]) >= 3:
                    record["until"] = now + 3600
                    record["reason"] = reason = "launch_budget_exhausted"
                    allowed = False
                else:
                    record["launches"].append(now)
            atomic_json(path, state)
            return allowed, reason


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("check", "observe", "launch", "park"))
    parser.add_argument("--state", required=True)
    parser.add_argument("--session", required=True)
    args = parser.parse_args()
    allowed, reason = LegacyGuard(args.state).transact(args.action, args.session, sys.stdin.read() if args.action == "observe" else "")
    if not allowed:
        print(reason)
    return 0 if allowed else 1


if __name__ == "__main__":
    raise SystemExit(main())
