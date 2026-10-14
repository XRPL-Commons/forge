#!/usr/bin/env python3
"""Reject accidentally tracked runtime/config/credential artifacts and private host details.

This complements a secret scanner; it does not recognize every confidential file.
Print paths, line numbers and reasons only, never matching file contents.
"""
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

FORBIDDEN_DIRS = {'state', 'logs', 'local', 'backups', 'credentials', 'archives', 'setup', '.claude', '.codex', 'node_modules', '__pycache__'}
FORBIDDEN_NAMES = {'runtime.json', 'host-runtime.json', 'factory.json', 'github-credentials.env', '.env', 'config.env'}
FORBIDDEN_SUFFIXES = {'.pem', '.key', '.sqlite', '.sqlite3', '.db', '.pyc', '.tar', '.gz', '.zip'}
# Synthetic JSON served by the UI's mock API; file names mirror endpoint paths, never live state.
SYNTHETIC_FIXTURES = PurePosixPath('ui/tests/e2e/fixtures')
# Private host details. Documented placeholders stay allowed: the `forge` service
# account, `example` tailnet names and the `you` macOS account.
PRIVATE_PATTERNS = (
    (re.compile(r'/home/([a-z_][a-z0-9_.-]*)/'), {'forge'}, 'personal /home/<user>/ path'),
    (re.compile(r'([A-Za-z0-9-]+)\.ts\.net\b'), {'example'}, 'private Tailscale *.ts.net name'),
    (re.compile(r'/Users/([A-Za-z][A-Za-z0-9._-]*)/'), {'you'}, 'personal /Users/<user>/ path'),
)


def path_findings(names):
    findings = []
    for name in names:
        path = PurePosixPath(name)
        if path.suffix == '.json' and path.parts[:len(SYNTHETIC_FIXTURES.parts)] == SYNTHETIC_FIXTURES.parts:
            continue
        example = path.name.endswith('.example')
        if (set(path.parts[:-1]) & FORBIDDEN_DIRS or path.name in FORBIDDEN_NAMES
                or path.suffix in FORBIDDEN_SUFFIXES or path.suffix == '.repo'
                or path.name.startswith('.env.') and not example):
            findings.append(f'{name}: runtime, credential, or private configuration artifact must not be tracked')
    return findings


def content_findings(root, names):
    findings = []
    for name in names:
        try:
            text = (root / name).read_bytes().decode()
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for pattern, allowed, reason in PRIVATE_PATTERNS:
                if any(match.group(1) not in allowed for match in pattern.finditer(line)):
                    findings.append(f'{name}:{number}: {reason}; use a placeholder')
    return findings


def main():
    root = Path(__file__).resolve().parents[1]
    names = [n for n in subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().split('\0') if n]
    artifacts = path_findings(names)
    private = content_findings(root, names)
    for finding in artifacts + private:
        print(finding)
    print(f'Checked {len(names)} tracked paths; {len(artifacts)} forbidden artifacts; {len(private)} private host details')
    return bool(artifacts or private)


if __name__ == '__main__':
    sys.exit(main())
