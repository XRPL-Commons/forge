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
    ('blocked_auth', re.compile(r'(?:not logged in|please (?:run /login|log in)|authentication (?:failed|error)|invalid api key|oauth token.*expired)', re.I)),
    ('blocked_quota', re.compile(r'(?:credit balance is too low|out of (?:extra usage|credits)|usage limit reached|you.ve hit your limit|insufficient_quota)', re.I)),
    ('blocked_permission', re.compile(r'(?:sandbox.*(?:failed|operation not permitted)|permission denied)', re.I)),
)
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


MODEL_LIMIT = re.compile(r"^⎿\s+You've reached your [A-Za-z][A-Za-z0-9 .-]{0,64} limit\. Run /usage-credits to continue or switch models with /model\.$")


def current_model_limit(text):
    """Recognize the observed provider banner as the latest response only."""
    lines = ANSI.sub('', text).replace('\u00a0', ' ').splitlines()
    # A literal code fence/blockquote in an assistant explanation is not the
    # rendered provider banner. Never scan past a later meaningful response.
    while lines:
        line = lines.pop().strip()
        if not line or line == '❯' or re.fullmatch(r'[─━═\-]+', line):
            continue
        if line.startswith('⏵⏵ bypass permissions on'):
            continue
        if re.match(r'^[✻✽✢✶✳] [^\r\n]{1,100} for\s*\d', line):
            continue
        if not MODEL_LIMIT.fullmatch(line):
            return None
        prior = '\n'.join(lines)
        if prior.count('```') % 2 or (lines and re.search(r'(?:review summary|quoted (?:message|error)|example):\s*$', lines[-1], re.I)):
            return None
        return line
    return None


def current_error_line(text):
    """Only a current standalone provider error, never arbitrary task prose."""
    from .handoff import idle_prompt
    if idle_prompt(text):
        return None  # A healthy current input footer makes prior errors history.
    lines = ANSI.sub('', text).replace('\u00a0', ' ').splitlines()
    while lines:
        line = lines.pop().strip()
        if not line or re.fullmatch(r'[─━═\-]+', line):
            continue
        if line == '❯' or line.startswith('⏵⏵ bypass permissions on'):
            continue
        # The last meaningful response must itself be the provider error.
        # No searching backwards through quoted text, tool output, or history.
        if line.startswith(('>', '`', '❯', '●')):
            return None
        # A review summary/tool response can quote even provider-looking lines.
        # With no structured provider attestation, accept only a standalone
        # error region following a submitted prompt, or a standalone banner.
        prior = [x.strip() for x in lines if x.strip() and not re.fullmatch(r'[─━═\-]+', x.strip())]
        prompts = [i for i,x in enumerate(prior) if x.startswith('❯')]
        prior = prior[prompts[-1] + 1:] if prompts else prior
        if prior:
            return None
        line = re.sub(r'^⎿\s*', '', line)
        prefixed = re.match(r'^(?:API Error|Authentication Error|Error):\s*(.+)$', line, re.I)
        if prefixed:
            return prefixed.group(1)
        if re.match(r"^(?:Not logged in|Please (?:run /login|log in)|Authentication (?:failed|error)|Invalid API key|OAuth token.*expired|Credit balance is too low|Out of (?:extra usage|credits)|Usage limit reached|You.ve hit your limit|Sandbox.*(?:failed|operation not permitted)|Permission denied)(?:[.!: \t]|$)", line, re.I):
            return line
        return None
    return None


def classify(text):
    if current_model_limit(text):
        return 'blocked_model_usage'
    line = current_error_line(text)
    if line:
        for kind, expression in ERRORS:
            if expression.search(line):
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
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False, 'guard_busy'
            try:
                state = json.loads(path.read_text()) if path.exists() else {"sessions": {}, "account_until": 0}
            except (ValueError, OSError):
                return False, "state_corrupt"
            record = state["sessions"].setdefault(session, {"until": 0, "launches": []})
            kind = classify(text) if action == "observe" else None
            # A newly submitted dispatch can produce a new identical error;
            # repeatedly observing the same dispatch's pane cannot extend it.
            try:
                submitted = (Path(self.directory).parent / (session + '.up')).read_text()
            except OSError:
                submitted = ''
            error_fingerprint = hashlib.sha256((submitted + '\n' + (current_model_limit(text) or current_error_line(text) or '')).encode()).hexdigest() if kind else None
            if kind and record.get('error_fingerprint') == error_fingerprint:
                # The same captured error cannot renew a cooldown forever.
                kind = None
            if kind:
                record['error_fingerprint'] = error_fingerprint
                record["reason"] = kind
                record["until"] = now + 3600
                # Terminal text cannot attest provider/account origin. Keep
                # this hold session-scoped; independently established existing
                # account guards remain honored and are never cleared here.
            if action == 'progress' and record.get('reason') == 'no_progress':
                record.pop('reason', None)
                record['until'] = 0
            if action == "park":
                record["reason"] = "no_progress"
                record["until"] = now + 3600
            if action == "start_failed" and record.get("reason") not in {
                    "blocked_auth", "blocked_quota", "blocked_model_usage", "blocked_permission", "launch_budget_exhausted"}:
                record["reason"] = "provider_start_failed"
                record["until"] = now + 60
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
    parser.add_argument("action", choices=("check", "observe", "launch", "park", "start_failed", "progress"))
    parser.add_argument("--state", required=True)
    parser.add_argument("--session", required=True)
    args = parser.parse_args()
    allowed, reason = LegacyGuard(args.state).transact(args.action, args.session, sys.stdin.read() if args.action == "observe" else "")
    if not allowed:
        print(reason)
    return 0 if allowed else 1


if __name__ == "__main__":
    raise SystemExit(main())
