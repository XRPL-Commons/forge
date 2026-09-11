#!/usr/bin/env python3
"""Reject accidentally tracked runtime/config/credential artifacts.

This complements a secret scanner; it does not recognize every confidential file.
Print paths and reasons only, never matching file contents.
"""
from pathlib import Path, PurePosixPath
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().split('\0')
    findings = []
    forbidden_dirs = {'state', 'logs', 'local', 'backups', '.claude', '.codex', 'node_modules', '__pycache__'}
    forbidden_names = {'factory.json', 'github-credentials.env', '.env', 'config.env'}
    forbidden_suffixes = {'.pem', '.key', '.sqlite', '.sqlite3', '.db', '.pyc', '.tar', '.gz', '.zip'}
    for name in names:
        if not name:
            continue
        path = PurePosixPath(name)
        example = path.name.endswith('.example')
        if (set(path.parts[:-1]) & forbidden_dirs or path.name in forbidden_names
                or path.suffix in forbidden_suffixes or path.suffix == '.repo'
                or path.name.startswith('.env.') and not example):
            findings.append(name)
    for name in findings:
        print(f'{name}: runtime, credential, or private configuration artifact must not be tracked')
    print(f'Checked {len([n for n in names if n])} tracked paths; {len(findings)} forbidden artifacts')
    return bool(findings)


if __name__ == '__main__':
    sys.exit(main())
