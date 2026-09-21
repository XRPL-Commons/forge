"""Codex adapter for the role-agnostic runner image. No terminal scraping."""
import json
import os
from pathlib import Path
import subprocess
import runpy


class PreparationError(RuntimeError):
    pass


def schema(note):
    props = {'version': {'type': 'integer', 'enum': [1]},
             'attempt': {'type': 'string', 'enum': [note['id']]},
             'role': {'type': 'string', 'enum': [note['role']]},
             'outcome': {'type': 'string', 'enum': ['pass', 'reject', 'unable', 'wait', 'blocked_environment']},
             'blocker': {'type': ['string', 'null'], 'enum': [None, 'toolchain', 'sandbox', 'dependency_access', 'resources', 'ci_evidence']},
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
        try:
            completed = subprocess.run(['git', *args], env=env, check=False, timeout=180, capture_output=True, text=True)
        except (OSError, subprocess.SubprocessError) as error:
            raise PreparationError('Repository preparation could not run') from error
        if completed.returncode:
            print(completed.stderr[-16384:].replace(env['GH_TOKEN'], '[REDACTED]'), flush=True)
            raise PreparationError('Repository preparation failed; check repository access, revision availability and disk space')
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
    from build_environment import environment
    env.update(environment(directory))
    exclude = directory / '.git/info/exclude'
    with exclude.open('a') as stream:
        stream.write('\n/.forge/\n/.home/\n')
    registry = runpy.run_path('/opt/forge/harnesses/registry.py')
    return registry['adapter'](note.get('provider', 'codex'))['run'](note, directory, env, schema(note))


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (PreparationError, OSError) as error:
        from build_environment import blocked
        note = json.loads(Path(os.environ['FORGE_ASSIGNMENT']).read_text())
        blocked(note, 'Forge could not prepare the disposable checkout before work started. Check repository access, revision availability and disk space.', 'dependency_access')
        raise SystemExit(125) from None
