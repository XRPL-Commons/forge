#!/usr/bin/env python3
"""Offline installation gate: build tools inside the actual native sandbox."""
import argparse
from pathlib import Path
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from forge.factory.runner import process_isolation


SCRIPT = r'''
import os, runpy, subprocess
from pathlib import Path
repo = Path('/work/repository'); repo.mkdir()
home = Path('/work/home/.codex'); home.mkdir(parents=True)
(home / 'auth.json').write_text('SYNTHETIC_PRIVATE_CANARY')
(repo / '.codex').mkdir(); (repo / '.codex/config.toml').write_text('# protected')
(repo / '.agents').mkdir(); (repo / '.agents/policy').write_text('protected')
private = Path('/work/outside-repository'); private.write_text('private')
subprocess.run(['git', 'init', str(repo)], check=True, capture_output=True)
runpy.run_path('/opt/forge/harness.py')['prepare_environment'](repo)
env = runpy.run_path('/opt/forge/harnesses/codex/adapter.py')['prepare_tool_environment'](repo, os.environ)
env['CODEX_HOME'] = str(home)
command = ['codex', 'sandbox', '-P', 'forge_runner', '--include-managed-config', '-C', str(repo),
           'python3', '/opt/forge/build_environment.py', '--check-environment']
subprocess.run(command, cwd=repo, env=env, check=True, timeout=90)
native = """
from pathlib import Path
import subprocess
for name in ('/work/home/.codex/auth.json', '/work/outside-repository'):
    try: Path(name).read_bytes()
    except (PermissionError, FileNotFoundError): pass
    else: raise RuntimeError('Private input was readable: ' + name)
# /proc contains sandbox-local processes. None may expose the outer harness
# environment or bypass hidden private files via a process-root alias.
for proc in Path('/proc').iterdir():
    if not proc.name.isdigit(): continue
    try:
        assert b'SYNTHETIC_PARENT_SECRET' not in (proc / 'environ').read_bytes(), 'Parent environment exposed'
    except (PermissionError, FileNotFoundError, ProcessLookupError): pass
    try: (proc / 'root/work/home/.codex/auth.json').read_bytes()
    except (PermissionError, FileNotFoundError, ProcessLookupError): pass
    else: raise RuntimeError('Private auth exposed through procfs')
for name in ('.codex/config.toml', '.agents/policy'):
    try: Path(name).write_text('forbidden')
    except (PermissionError, OSError): pass
    else: raise RuntimeError('Protected configuration was writable')
Path('fixture.txt').write_text('fixture')
subprocess.run(['git', 'add', 'fixture.txt'], check=True)
subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'fixture'], check=True)
print('forge-isolation-ok')
"""
command[-3:] = ['python3', '-c', native]
subprocess.run(command, cwd=repo, env=env, check=True, timeout=30)
assert private.read_text() == 'private'
'''


def validate(image, seccomp, apparmor=None):
    name = 'forge-build-canary-' + uuid.uuid4().hex
    argv = ['docker', 'run', '--rm', '--name', name, '-i', '--network', 'none',
            '--memory', '2g', '--memory-swap', '2g', '--cpus', '2', '--pids-limit', '1024',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--security-opt', 'seccomp=' + str(seccomp),
            '--read-only', '--user', '1000:1000', '--env', 'HOME=/work/home',
            '--env', 'FORGE_PROBE_SECRET=SYNTHETIC_PARENT_SECRET',
            '--tmpfs', '/work:uid=1000,gid=1000,size=512m', '--tmpfs', '/tmp:rw,nosuid,nodev,size=32m']
    argv += process_isolation({'provider': 'codex', 'apparmor_profile': apparmor})
    try:
        result = subprocess.run(argv + [image, 'python3', '-I', '-'], input=SCRIPT, text=True,
                                capture_output=True, timeout=120)
        print(result.stdout); print(result.stderr)
        if result.returncode or any(marker not in result.stdout for marker in
                                    ('forge-build-environment-ok', 'forge-browser-ok', 'forge-isolation-ok')):
            raise RuntimeError('Runner build-environment canary failed')
    finally:
        subprocess.run(['docker', 'rm', '--force', name], capture_output=True, timeout=15)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--seccomp', type=Path, required=True)
    parser.add_argument('--apparmor')
    args = parser.parse_args()
    validate(args.image, args.seccomp, args.apparmor)
