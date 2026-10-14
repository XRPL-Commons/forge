"""Codex adapter: native sandbox, structured result and connection probe."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import runpy

RESULTS = runpy.run_path(str(Path(__file__).resolve().parent.parent / 'results.py'))
EXECUTION = runpy.run_path(str(Path(__file__).resolve().parent.parent / 'execution.py'))


def prepare_tool_environment(directory, env):
    client = Path('/work/home/.codex'); client.mkdir(parents=True, exist_ok=True)
    # Repository commands receive public mirror/proxy settings. Provider
    # credentials stay in the parent harness, behind the
    # image-owned deny-read policy, and never enter shell subprocess environments.
    child_env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(directory / '.forge/home'),
                 'GIT_TERMINAL_PROMPT': '0', 'GIT_CONFIG_GLOBAL': '/opt/forge/gitconfig', 'GIT_CONFIG_NOSYSTEM': '1',
                 'HTTP_PROXY': env.get('HTTP_PROXY', ''), 'HTTPS_PROXY': env.get('HTTPS_PROXY', '')}
    child_env.update({key: env[key] for key in ('NO_PROXY', 'no_proxy', 'npm_config_registry', 'PIP_INDEX_URL', 'PIP_TRUSTED_HOST') if key in env})
    build = runpy.run_path('/opt/forge/build_environment.py')
    child_env.update(build['environment'](directory, [s for s in env.get('FORGE_REQUIRED_SERVICES', '').split(',') if s]))
    Path(child_env['HOME']).mkdir(mode=0o700, exist_ok=True)
    config = 'default_permissions = "forge_runner"\napproval_policy = "never"\nweb_search = "disabled"\n[shell_environment_policy]\ninherit = "none"\n[shell_environment_policy.set]\n'
    config += '\n'.join(key + ' = ' + json.dumps(value) for key, value in child_env.items()) + '\n'
    (client / 'config.toml').write_text(config)
    return child_env


def run(note, directory, env, result_schema):
    path = Path('/work/result-schema.json')
    path.write_text(json.dumps(result_schema))
    client = Path('/work/home/.codex')
    child_env = prepare_tool_environment(directory, env)
    build = runpy.run_path('/opt/forge/build_environment.py')
    # No model request and no PR mutation until the actual tool sandbox works.
    try:
        checked = subprocess.run(['codex', 'sandbox', '-P', 'forge_runner', '--include-managed-config',
                                  '-C', str(directory), 'python3', '/opt/forge/build_environment.py', '--check-environment'],
                                 env={**child_env, 'CODEX_HOME': str(client)}, cwd=directory,
                                 capture_output=True, text=True, timeout=120)
        if checked.returncode:
            raise RuntimeError('Native toolchain check failed')
    except (OSError, RuntimeError, subprocess.SubprocessError):
        # Raw diagnostics can include registry credentials from repository config.
        # A fixed report is safe; installation's credential-free canary provides
        # full diagnostics for repairing the image.
        build['blocked'](note, 'Forge native toolchain readiness failed before work started. Run deploy/test_build_environment.py against the configured image; the candidate PR was not changed.')
        return 125
    # The native sandbox stays enabled. Image provisioning must prove it works;
    # an unavailable sandbox fails the attempt instead of disabling protection.
    args = ['codex', '-a', 'never', 'exec', '--json',
            '--output-schema', str(path), '--output-last-message', '/work/final-result.json',
            '--cd', str(directory), '-']
    if note.get('model'):
        args[5:5] = ['--model', note['model']]
    prompt = json.dumps(note) + '\nReturn your result as the final structured response. It will be validated by the supervisor.'
    code, events = EXECUTION['stream'](args, prompt, directory, env)
    if code:
        provider = EXECUTION['codex_provider_error'](events)
        if provider is not None:
            EXECUTION['write_receipt'](note, 'provider_error', provider_error=provider)
    output = Path('/work/final-result.json')
    RESULTS['publish_result'](note, output)
    return code


def probe(note, directory):
    repo = directory
    home = Path('/work/home/.codex'); home.mkdir(parents=True, exist_ok=True)
    child_env = prepare_tool_environment(repo, os.environ)
    private = Path('/work/home/probe-private'); private.write_text('synthetic probe')
    Path('/work/home/forge-exit.json').write_text('synthetic private envelope')
    # An editable worktree is insufficient: Codex protects .git separately.
    # Prove a real local commit succeeds before reporting the runner ready.
    subprocess.run(['git', 'init', str(repo)], check=True, capture_output=True, timeout=10)
    command = ['codex', 'sandbox', '-P', 'forge_runner', '--include-managed-config', '-C', str(repo), '/usr/local/bin/python3', '-c',
               'from pathlib import Path; p=Path("/work/home/forge-exit.json"); '
               '\ntry: p.write_text("forged")\nexcept (PermissionError, FileNotFoundError): pass\nelse: raise SystemExit(4)\n'
               'try: p.read_bytes()\nexcept (PermissionError, FileNotFoundError): pass\nelse: raise SystemExit(5)\n'
               'try: p.rename("/work/repository/stolen-envelope")\nexcept (PermissionError, FileNotFoundError): pass\nelse: raise SystemExit(6)\n'
               'p=Path("/work/home/probe-private"); '
               '\ntry: p.read_text()\nexcept (PermissionError, FileNotFoundError): pass\nelse: raise SystemExit(2)\n'
               'p=Path("/work/home/.codex/auth.json")\n'
               'try: p.read_text()\nexcept (PermissionError, FileNotFoundError): pass\nelse: raise SystemExit(3)\n'
               'Path("/work/repository/probe-write").write_text("ok")\n'
               'import subprocess\n'
               'subprocess.run(["git", "add", "probe-write"], check=True)\n'
               'subprocess.run(["git", "-c", "user.name=Forge probe", "-c", "user.email=probe@example.invalid", "commit", "-m", "Readiness probe"], check=True)']
    checked = subprocess.run(command, cwd=repo, capture_output=True, timeout=30)
    if checked.returncode or not (repo / 'probe-write').is_file():
        Path('/work/result.json').write_text(json.dumps({'ready': False, 'attempt': note['id'], 'reason': 'runner_environment_unavailable'}))
        raise RuntimeError('Native sandbox probe failed')
    checked = subprocess.run(command[:command.index('/usr/local/bin/python3')] +
                             ['python3', '/opt/forge/build_environment.py', '--check-environment'], cwd=repo,
                             env={**child_env, 'CODEX_HOME': str(home)}, capture_output=True, timeout=120)
    if checked.returncode:
        Path('/work/result.json').write_text(json.dumps({'ready': False, 'attempt': note['id'], 'reason': 'runner_environment_unavailable'}))
        raise RuntimeError('Native build environment probe failed')
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
