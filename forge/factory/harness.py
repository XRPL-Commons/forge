"""Codex adapter for the role-agnostic runner image. No terminal scraping."""
import json
import os
from pathlib import Path
import subprocess


def schema(note):
    props = {'version': {'type': 'integer', 'enum': [1]},
             'attempt': {'type': 'string', 'enum': [note['id']]},
             'role': {'type': 'string', 'enum': [note['role']]},
             'outcome': {'type': 'string', 'enum': ['pass', 'reject', 'unable', 'wait']},
             'summary': {'type': 'string'}}
    if note['role'] in ('review', 'audit'):
        props['candidate'] = {'type': 'string', 'enum': [note['candidate']['key']]}
    if note['role'] == 'audit':
        props.update(human_required={'type': 'string', 'enum': ['required', 'not_required', 'unknown']}, human_reason={'type': 'string'})
    if note['role'] == 'recovery':
        props.update(pattern={'type': 'string'}, evidence={'type': 'array', 'items': {'type': 'string'}},
                     uncertainty={'type': 'string'}, alternative={'anyOf': [{'type': 'null'}, {
                         'type': 'object', 'properties': {k: {'type': 'string'} for k in ('change', 'reason', 'evidence')},
                         'required': ['change', 'reason', 'evidence'], 'additionalProperties': False}]})
    return {'type': 'object', 'properties': props, 'required': list(props), 'additionalProperties': False}


def main():
    note = json.loads(Path(os.environ['FORGE_ASSIGNMENT']).read_text())
    directory = Path('/work/repository')
    # Token never enters a remote URL, git configuration, argument or image layer.
    askpass = Path('/work/askpass')
    askpass.write_text('#!/usr/local/bin/python3\nimport os,sys\nprint("x-access-token" if "Username" in sys.argv[1] else os.environ["GH_TOKEN"])\n')
    askpass.chmod(0o700)
    env = {**os.environ, 'GIT_ASKPASS': str(askpass), 'GIT_TERMINAL_PROMPT': '0',
           'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'}
    def git(*args):
        completed = subprocess.run(['git', *args], env=env, check=False, timeout=180, capture_output=True, text=True)
        if completed.returncode:
            print(completed.stderr[-16384:].replace(env['GH_TOKEN'], '[REDACTED]'), flush=True)
            raise RuntimeError('Git operation failed; see captured diagnostic output')
        return completed
    git('clone', '--no-checkout', 'https://github.com/' + note['repo'] + '.git', str(directory))
    git('-C', str(directory), 'config', 'user.name', note['publisher'])
    git('-C', str(directory), 'config', 'user.email', note['publisher'] + '@users.noreply.github.com')
    selected = note.get('candidate')
    if selected:
        git('-C', str(directory), 'fetch', 'origin', selected['head'])
        git('-C', str(directory), 'checkout', '--detach', selected['head'])
    else:
        git('-C', str(directory), 'checkout', '--detach', 'origin/HEAD')
    if note['role'] == 'build':
        git('-C', str(directory), 'checkout', '-B', note['branch'])
    path = Path('/work/result-schema.json')
    path.write_text(json.dumps(schema(note)))
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
    prompt = json.dumps(note) + '\nReturn your result as the final structured response. It will be validated by the supervisor.'
    completed = subprocess.run(args, input=prompt, text=True, env=env, check=False)
    output = Path('/work/final-result.json')
    if output.is_file() and output.stat().st_size <= 65536:
        # Atomic rename is the result handoff. Partial provider output is not a result.
        json.loads(output.read_text())
        output.replace('/work/result.json')
    return completed.returncode


if __name__ == '__main__':
    raise SystemExit(main())
