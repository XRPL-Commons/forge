"""Codex adapter: native sandbox, structured result and connection probe."""
import json
import os
from pathlib import Path
import subprocess
import tempfile


def run(note, directory, env, result_schema):
    askpass = Path("/work/askpass")
    path = Path("/work/result-schema.json")
    path.write_text(json.dumps(result_schema))
    client = Path('/work/home/.codex'); client.mkdir(parents=True, exist_ok=True)
    # Repository commands receive only their job's GitHub token and public proxy
    # settings. Provider credentials stay in the parent harness, behind the
    # image-owned deny-read policy, and never enter shell subprocess environments.
    child_env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': '/work/repository/.home',
                 'GH_TOKEN': env['GH_TOKEN'], 'GIT_ASKPASS': str(askpass), 'GIT_TERMINAL_PROMPT': '0',
                 'HTTP_PROXY': env.get('HTTP_PROXY', ''), 'HTTPS_PROXY': env.get('HTTPS_PROXY', '')}
    Path(child_env['HOME']).mkdir(mode=0o700, exist_ok=True)
    config = 'default_permissions = "forge_runner"\napproval_policy = "never"\nweb_search = "disabled"\n[shell_environment_policy]\ninherit = "none"\n[shell_environment_policy.set]\n'
    config += '\n'.join(key + ' = ' + json.dumps(value) for key, value in child_env.items()) + '\n'
    (client / 'config.toml').write_text(config)
    # The native sandbox stays enabled. Image provisioning must prove it works;
    # an unavailable sandbox fails the attempt instead of disabling protection.
    args = ['codex', '-a', 'never', 'exec', '--json',
            '--output-schema', str(path), '--output-last-message', '/work/final-result.json',
            '--cd', str(directory), '-']
    if note.get('model'):
        args[5:5] = ['--model', note['model']]
    prompt = json.dumps(note) + '\nReturn your result as the final structured response. It will be validated by the supervisor.'
    completed = subprocess.run(args, input=prompt, text=True, env=env, check=False)
    output = Path('/work/final-result.json')
    if output.is_file() and output.stat().st_size <= 65536:
        # Atomic rename is the result handoff. Partial provider output is not a result.
        json.loads(output.read_text())
        output.replace('/work/result.json')
    return completed.returncode


def probe(note, directory):
    repo = directory
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
                                 *(['--model', note['model']] if note.get('model') else []),
                                 '--output-schema', '/work/probe-schema.json', '--output-last-message', '/work/probe-answer.json',
                                 '--cd', str(repo), '-'],
                                input='This is a connection check. Do not call any tools. Return ready true.',
                                text=True, stdout=diagnostic, stderr=subprocess.STDOUT, timeout=120)
        diagnostic.seek(0)
        text = diagnostic.read(65536).lower()
        Path('/work/home/harness-diagnostic.json').write_text(text)
        if result.returncode:
            # Fixed labels only. Provider output and credential values never leave this probe.
            reason = ('provider_login_expired' if any(v in text for v in ('refresh_token_reused', 'token_expired', 'sign in again', 'refresh token'))
                      else 'provider_connection_failed')
            Path('/work/result.json').write_text(json.dumps({'ready': False, 'attempt': note['id'], 'reason': reason}))
            raise RuntimeError(reason)
    if result.returncode or json.loads(Path('/work/probe-answer.json').read_text()) != {'ready': True}:
        raise RuntimeError('Provider authentication or model check failed')
    Path('/work/result.json').write_text(json.dumps({'ready': True, 'attempt': note['id']}))
