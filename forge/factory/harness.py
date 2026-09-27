"""Codex adapter for the role-agnostic runner image. No terminal scraping."""
import json
import os
from pathlib import Path
import subprocess
import runpy


class PreparationError(RuntimeError):
    pass


def prepare_environment(directory):
    # The entrypoint uses Python -I: sibling modules are deliberately not on
    # sys.path. Load only this immutable, image-owned absolute path.
    build = runpy.run_path('/opt/forge/build_environment.py')
    env = build['environment'](directory)
    with (directory / '.git/info/exclude').open('a') as stream:
        stream.write('\n/.forge/\n/.home/\n')
    return env


# Kept in step with contracts.HEADLINE_LIMIT / PLAIN_LIMIT, which truncate what
# arrives. The image loads no sibling module, so the numbers live on both sides.
HEADLINE_LIMIT, PLAIN_LIMIT = 90, 360


def role_properties(note):
    """Role-specific result fields: a fifth role is one line here."""
    candidate = {'candidate': {'type': 'string', 'enum': [(note.get('candidate') or {}).get('key')]}}
    return {
        'build': {},
        'discussion': {'proposal': {'anyOf': [{'type': 'null'}, {'type': 'object', 'properties': {
            'action': {'type': 'string', 'enum': ['retry', 'resume', 'recheck', 'queue', 'pause', 'backlog']},
            'reason': {'type': 'string', 'maxLength': 2000}}, 'required': ['action', 'reason'], 'additionalProperties': False}]}},
        'review': candidate,
        'audit': {**candidate, 'human_required': {'type': 'string', 'enum': ['required', 'not_required', 'unknown']},
                  'human_reason': {'type': 'string'}},
        'recovery': {'pattern': {'type': 'string'}, 'evidence': {'type': 'array', 'items': {'type': 'string'}},
                     'uncertainty': {'type': 'string'}, 'alternative': {'anyOf': [{'type': 'null'}, {
                         'type': 'object', 'properties': {k: {'type': 'string'} for k in ('change', 'reason', 'evidence')},
                         'required': ['change', 'reason', 'evidence'], 'additionalProperties': False}]}},
        'strategist': strategist_properties(),
    }.get(note['role'], {})


def strategist_properties():
    nullable = lambda t: {'type': [t, 'null']}
    return {
        'strategy': {'type': 'string', 'enum': ['github_deployments', 'health', 'header', 'unobservable']},
        'deploy': {'type': 'object', 'properties': {'workflow': nullable('string'), 'environment': nullable('string'), 'trigger': nullable('string')},
                   'required': ['workflow', 'environment', 'trigger'], 'additionalProperties': False},
        'domain': {'type': 'object', 'properties': {'value': nullable('string'), 'source': {'type': 'string', 'enum': ['workflow', 'docs', 'config', 'none']},
                   'confidence': {'type': 'string', 'enum': ['high', 'low', 'none']}}, 'required': ['value', 'source', 'confidence'], 'additionalProperties': False},
        'exposure': {'type': 'object', 'properties': {'kind': {'type': 'string', 'enum': ['health', 'header', 'none']}, 'path': nullable('string'),
                     'field': nullable('string'), 'header': nullable('string')}, 'required': ['kind', 'path', 'field', 'header'], 'additionalProperties': False},
        'commit_source': {'type': 'string'},
        'proposal': {'anyOf': [{'type': 'null'}, {'type': 'object', 'properties': {'title': {'type': 'string'}, 'body': {'type': 'string'}},
                     'required': ['title', 'body'], 'additionalProperties': False}]},
    }


def schema(note):
    props = {'version': {'type': 'integer', 'enum': [1]},
             'attempt': {'type': 'string', 'enum': [note['id']]},
             'role': {'type': 'string', 'enum': [note['role']]},
             'outcome': {'type': 'string', 'enum': ['pass', 'reject', 'unable', 'wait', 'blocked_environment']},
             'blocker': {'type': ['string', 'null'], 'enum': [None, 'toolchain', 'sandbox', 'dependency_access', 'resources', 'ci_evidence']},
             'summary': {'type': 'string'},
             # Plain language for a reader who does not read code, in every role.
             'headline': {'type': 'string', 'maxLength': HEADLINE_LIMIT},
             'plain': {'type': 'string', 'maxLength': PLAIN_LIMIT}}
    props.update(role_properties(note))
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
    if note['role'] == 'build' and note.get('resume_branch'):
        # A memory retry may have pushed newer work than the last accepted
        # candidate. Resume that branch, preserving the PR and published commits.
        ref = 'refs/heads/' + note['resume_branch']
        published = git('-C', str(directory), 'ls-remote', '--heads', 'origin', ref).stdout.strip()
        if published:
            git('-C', str(directory), 'fetch', 'origin', ref)
            git('-C', str(directory), 'checkout', '--detach', 'FETCH_HEAD')
        elif selected:
            git('-C', str(directory), 'fetch', 'origin', selected['head'])
            git('-C', str(directory), 'checkout', '--detach', selected['head'])
        else:
            git('-C', str(directory), 'checkout', '--detach', 'origin/HEAD')
    elif selected:
        git('-C', str(directory), 'fetch', 'origin', selected['head'])
        git('-C', str(directory), 'checkout', '--detach', selected['head'])
    elif note['role'] == 'strategist':
        # Read-only assignment: detach at the given head, never create a branch.
        git('-C', str(directory), 'fetch', 'origin', note['head'])
        git('-C', str(directory), 'checkout', '--detach', note['head'])
    else:
        git('-C', str(directory), 'checkout', '--detach', 'origin/HEAD')
    if note['role'] == 'build':
        git('-C', str(directory), 'checkout', '-B', note['branch'])
    env.update(prepare_environment(directory))
    registry = runpy.run_path('/opt/forge/harnesses/registry.py')
    return registry['adapter'](note.get('provider', 'codex'))['run'](note, directory, env, schema(note))


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (PreparationError, OSError) as error:
        note = json.loads(Path(os.environ['FORGE_ASSIGNMENT']).read_text())
        runpy.run_path('/opt/forge/build_environment.py')['blocked'](note, 'Forge could not prepare the disposable checkout before work started. Check repository access, revision availability and disk space.', 'dependency_access')
        raise SystemExit(125) from None
