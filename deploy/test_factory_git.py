#!/usr/bin/env python3
"""Credential-free Git commit and isolation check in the pinned runner image."""
import argparse
import subprocess
import uuid


SCRIPT = r'''
import os
from pathlib import Path
import subprocess

repo = Path('/work/repository'); repo.mkdir()
home = Path('/work/home/.codex'); home.mkdir(parents=True)
(home / 'config.toml').write_text('default_permissions = "forge_runner"\n')
(home / 'auth.json').write_text('SYNTHETIC_PRIVATE_CANARY')
outside = Path('/work/outside-repository')
outside.write_text('SYNTHETIC_OUTSIDE_CANARY')
(repo / '.codex').mkdir()
(repo / '.codex/config.toml').write_text('# protected repository configuration\n')
(repo / '.agents').mkdir()
(repo / '.agents/policy').write_text('protected')
subprocess.run(['git', 'init', str(repo)], check=True, capture_output=True)
command = r"""
set -eu
printf fixture > file.txt
git add file.txt
git -c user.name=Fixture -c user.email=fixture@example.invalid commit -m fixture
test -n "$(git rev-parse HEAD)"
if cat /work/home/.codex/auth.json >/dev/null 2>&1; then exit 91; fi
if cat /work/outside-repository >/dev/null 2>&1; then exit 92; fi
# Bubblewrap may provide an empty, private parent directory. A write there
# must never change the actual file outside the allowed repository.
printf forbidden > /work/outside-repository 2>/dev/null || true
if printf forbidden > .codex/config.toml 2>/dev/null; then exit 93; fi
if printf forbidden > .agents/policy 2>/dev/null; then exit 94; fi
echo git-and-isolation-ok
"""
result = subprocess.run(['codex', 'sandbox', '-P', 'forge_runner', '--include-managed-config',
                         '-C', str(repo), '/bin/sh', '-c', command], cwd=repo)
assert outside.read_text() == 'SYNTHETIC_OUTSIDE_CANARY', 'Outside file was changed'
raise SystemExit(result.returncode)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--seccomp', required=True)
    parser.add_argument('--apparmor', default='forge-runner-private-proc')
    args = parser.parse_args()
    name = 'forge-git-canary-' + uuid.uuid4().hex
    command = ['docker', 'run', '--rm', '--name', name, '-i', '--network', 'none',
               '--memory', '256m', '--cpus', '1', '--pids-limit', '128', '--cap-drop', 'ALL',
               '--security-opt', 'no-new-privileges', '--security-opt', 'apparmor=' + args.apparmor,
               '--security-opt', 'seccomp=' + args.seccomp, '--read-only', '--user', '1000:1000',
               '--env', 'HOME=/work/home', '--tmpfs', '/work:uid=1000,gid=1000,size=32m',
               '--tmpfs', '/tmp:rw,nosuid,nodev,size=32m', args.image, 'python3', '-']
    try:
        result = subprocess.run(command, input=SCRIPT, text=True, capture_output=True, timeout=60)
        print(result.stdout); print(result.stderr)
        if result.returncode or 'git-and-isolation-ok' not in result.stdout:
            raise RuntimeError('Git commit or isolation canary failed: exit ' + str(result.returncode))
        print('Git commit and isolation canary passed; no network or real credentials used.')
    finally:
        subprocess.run(['docker', 'rm', '--force', name], capture_output=True, timeout=15)


if __name__ == '__main__':
    main()
