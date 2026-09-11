#!/usr/bin/env python3
"""Credential-free native sandbox canary with an ephemeral named AppArmor policy.

Requires a reviewed seccomp JSON argument and permission to load/remove only the
new unique test profile. Never changes sysctls or existing profiles/services.
"""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import uuid


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',required=True);parser.add_argument('--seccomp',required=True)
    args=parser.parse_args()
    name='forge-native-test-'+uuid.uuid4().hex
    template=(Path(__file__).parent/'runner/forge-runner-native.apparmor').read_text()
    script = """import pathlib,subprocess
home=pathlib.Path('/work/home/.codex');home.mkdir(parents=True)
repo=pathlib.Path('/work/repository');repo.mkdir()
(home/'config.toml').write_text('default_permissions = "forge_runner"\\n')
(home/'private-canary').write_text('SYNTHETIC_PROVIDER_CREDENTIAL')
command='printf sandbox-ok > /work/repository/proof && cat /work/repository/proof; if cat /work/home/.codex/private-canary; then exit 98; fi; if printf forbidden > /outside/forbidden; then exit 99; fi'
p=subprocess.run(['codex','sandbox','-P','forge_runner','--include-managed-config','-C',str(repo),'/bin/sh','-c',command])
raise SystemExit(p.returncode)
"""
    with tempfile.TemporaryDirectory(prefix='forge-native-policy-') as temporary:
        policy=Path(temporary)/'profile';policy.write_text(template.replace('forge-runner-native',name))
        subprocess.run(['sudo','-n','apparmor_parser','-a',str(policy)],check=True,timeout=15)
        try:
            command=['docker','run','--rm','--name',name,'-i','--network','none','--cap-drop','ALL',
                     '--security-opt','no-new-privileges','--security-opt','apparmor='+name,
                     '--security-opt','seccomp='+str(Path(args.seccomp).resolve()),'--read-only','--user','1000:1000',
                     '--env','HOME=/work/home','--tmpfs','/work:uid=1000,gid=1000,size=16m',
                     '--tmpfs','/outside:uid=1000,gid=1000,size=16m','--tmpfs','/tmp:rw,nosuid,nodev,size=32m',args.image,'python3','-']
            outcome=subprocess.run(command,input=script,text=True,capture_output=True,timeout=45)
            print(outcome.stdout);print(outcome.stderr)
            if outcome.returncode or 'sandbox-ok' not in outcome.stdout:
                raise RuntimeError('Native sandbox did not demonstrate allowed work and outside-write denial')
            print('Native sandbox canary passed without credentials or global policy changes.')
        finally:
            subprocess.run(['docker','rm','--force',name],capture_output=True,timeout=15)
            subprocess.run(['sudo','-n','apparmor_parser','-R',str(policy)],check=True,timeout=15)


if __name__ == '__main__': main()
