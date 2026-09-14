"""Image-owned setup probe: verify native isolation and one real provider response."""
import json
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    note = json.loads(Path('/work/assignment.json').read_text())
    repo = Path('/work/repository'); repo.mkdir(exist_ok=True)
    home = Path('/work/home/.codex'); home.mkdir(parents=True, exist_ok=True)
    (home / 'config.toml').write_text('default_permissions = "forge_runner"\napproval_policy = "never"\nweb_search = "disabled"\n[shell_environment_policy]\ninherit = "none"\n')
    private = Path('/work/home/probe-private'); private.write_text('synthetic probe')
    command = ['codex', 'sandbox', '-P', 'forge_runner', '--include-managed-config', '-C', str(repo), '/usr/local/bin/python3', '-c',
               'from pathlib import Path; p=Path("/work/home/probe-private"); '
               '\ntry: p.read_text()\nexcept (PermissionError, FileNotFoundError): pass\nelse: raise SystemExit(2)\n'
               'p=Path("/work/home/.codex/auth.json")\n'
               'try: p.read_text()\nexcept (PermissionError, FileNotFoundError): pass\nelse: raise SystemExit(3)\n'
               'Path("/work/repository/probe-write").write_text("ok")']
    checked = subprocess.run(command, cwd=repo, capture_output=True, timeout=30)
    if checked.returncode or not (repo / 'probe-write').is_file():
        raise RuntimeError('Native sandbox probe failed')
    schema = {'type': 'object', 'properties': {'ready': {'type': 'boolean', 'enum': [True]}},
              'required': ['ready'], 'additionalProperties': False}
    Path('/work/probe-schema.json').write_text(json.dumps(schema))
    with tempfile.TemporaryFile(mode='w+') as diagnostic:
        result = subprocess.run(['codex', '-a', 'never', 'exec', '--json', '--skip-git-repo-check',
                                 '--output-schema', '/work/probe-schema.json', '--output-last-message', '/work/probe-answer.json',
                                 '--cd', str(repo), '-'],
                                input='This is a connection check. Do not call any tools. Return ready true.',
                                text=True, stdout=subprocess.DEVNULL, stderr=diagnostic, timeout=120)
        diagnostic.seek(0)
        text = diagnostic.read(65536).lower()
        if result.returncode:
            # Fixed labels only. Provider output and credential values never leave this probe.
            reason = ('provider_login_expired' if any(v in text for v in ('refresh_token_reused', 'token_expired', 'sign in again', 'refresh token'))
                      else 'provider_connection_failed')
            Path('/work/result.json').write_text(json.dumps({'ready': False, 'attempt': note['id'], 'reason': reason}))
            raise RuntimeError(reason)
    if result.returncode or json.loads(Path('/work/probe-answer.json').read_text()) != {'ready': True}:
        raise RuntimeError('Provider authentication or model check failed')
    Path('/work/result.json').write_text(json.dumps({'ready': True, 'attempt': note['id']}))


if __name__ == '__main__':
    main()
