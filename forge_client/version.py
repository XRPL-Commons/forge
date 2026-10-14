"""Revision reporting and atomic managed-client updates."""
import importlib.metadata
import json
from pathlib import Path
import re
import sys
import tempfile
import urllib.request

from .contracts import require
from .github import run_bounded

SHA = re.compile(r'[a-f0-9]{40}')


def client_revision():
    match = re.fullmatch(r'([a-f0-9]{40})-[a-f0-9]{32}', Path(sys.prefix).name)
    if match:
        return match[1]
    try:
        value = json.loads(importlib.metadata.distribution('forge-agent-client').read_text('direct_url.json') or '{}')
        commit = value.get('vcs_info', {}).get('commit_id', '')
        if SHA.fullmatch(commit):
            return commit
    except (importlib.metadata.PackageNotFoundError, ValueError, TypeError):
        pass
    source = Path(__file__).resolve().parents[1]
    if (source / '.git').exists():
        try:
            commit = run_bounded(['git', '-C', str(source), 'rev-parse', 'HEAD']).decode().strip()
            if SHA.fullmatch(commit):
                return commit
        except Exception:
            pass
    return None


def report(ssh=None):
    client = client_revision()
    server = None
    if ssh:
        from .install import Installation
        installation = Installation(ssh)
        state = installation.journal.read() or {}
        installation.host.fingerprint = state.get('host_key')
        program = '''import json, subprocess
def main(payload):
    result = subprocess.run(['runuser','-u','forge','--','env','-i','PATH=/usr/sbin:/usr/bin:/sbin:/bin',
                             'HOME=/home/forge','git','-C','/home/forge/forge','rev-parse','HEAD'],
                            capture_output=True, text=True, timeout=10)
    print(json.dumps({'ok':True,'revision':result.stdout.strip() if result.returncode == 0 else None}))
    return 0
'''
        value = installation.host.rpc(program, {'version': 1}, timeout=30)
        server = value.get('revision')
        require(server is None or isinstance(server, str) and SHA.fullmatch(server), 'invalid_server_revision')
    return {'client_revision': client, 'server_revision': server, 'ssh': ssh,
            'compatible': None if not client or not server else client == server,
            'warning': 'Client and server revisions differ; update the client to the selected server revision before setup.'
                       if client and server and client != server else None}


def update(*, revision=None, adopt=False):
    from .install import revision as resolve_revision
    commit = resolve_revision(revision)
    url = 'https://raw.githubusercontent.com/XRPL-Commons/forge/' + commit + '/install-cli.sh'
    with urllib.request.urlopen(url, timeout=30) as response:
        require(response.geturl() == url, 'installer_source_changed', 'The pinned installer source redirected.')
        data = response.read(262145)
    require(0 < len(data) <= 262144 and data.startswith(b'#!/bin/sh\n'), 'invalid_installer')
    with tempfile.TemporaryDirectory(prefix='forge-client-update-') as directory:
        path = Path(directory) / 'install-cli.sh'
        path.write_bytes(data)
        args = ['sh', str(path), '--revision', commit]
        if adopt:
            args.append('--adopt')
        run_bounded(args, environ={key: value for key, value in __import__('os').environ.items()
                                  if key not in ('GH_TOKEN', 'GITHUB_TOKEN')}, timeout=900)
    return {'status': 'updated', 'revision': commit, 'command': str(Path.home() / '.local/bin/forge')}
