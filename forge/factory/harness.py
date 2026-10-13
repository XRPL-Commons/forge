"""Codex adapter for the role-agnostic runner image. No terminal scraping."""
import json
import re
import os
from pathlib import Path
import subprocess
import runpy
from urllib.request import Request, urlopen
from urllib.error import URLError

# The worker image ships this file alone (deploy/runner/Dockerfile), so it cannot
# import the forge package. Kept equal to delegation_contracts.PLANNING_OUTCOMES by tests.
PLANNING_OUTCOMES = ('delegate', 'needs_decision', 'waiting_on_issues', 'blocked_environment', 'unable', 'wait', 'reject')


class PreparationError(RuntimeError):
    pass


class AssignmentChanged(PreparationError):
    pass


def begin_agent(env):
    """Wait until the image parent irreversibly records the phase boundary."""
    descriptor = os.environ.pop('FORGE_STARTUP_FD', None)
    if descriptor is not None:
        os.write(int(descriptor), b'agent_started\n')
        os.close(int(descriptor))
        acknowledgement = int(os.environ.pop('FORGE_STARTUP_ACK_FD'))
        try:
            if os.read(acknowledgement, 1) != b'1':
                raise RuntimeError('Runner did not acknowledge the agent boundary')
        finally:
            os.close(acknowledgement)
    env.pop('FORGE_STARTUP_FD', None)
    env.pop('FORGE_STARTUP_ACK_FD', None)


def check_publication_target(note, env):
    if note.get('builder_stage') != 'integration' or not note.get('resume_pr'):
        return
    if not note.get('resume_head') or not note.get('publication_author'):
        raise AssignmentChanged('The existing pull request needs a fresh publication approval')
    request = Request('https://api.github.com/repos/' + note['repo'] + '/pulls/' + str(note['resume_pr']),
                      headers={'Authorization': 'Bearer ' + env['GH_TOKEN'], 'Accept': 'application/vnd.github+json'})
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read(262145)
        if len(raw) > 262144:
            raise PreparationError('Existing publication metadata exceeds its bound')
        pr = json.loads(raw)
    except (URLError, ValueError) as error:
        raise PreparationError('Existing publication identity could not be checked') from error
    if (pr.get('number') != note['resume_pr'] or pr.get('state') != 'open' or pr.get('merged')
            or {key: pr.get('user', {}).get(key) for key in ('id', 'login', 'type')} != note['publication_author']
            or pr.get('head', {}).get('sha') != note['resume_head']
            or pr.get('head', {}).get('ref') != note['resume_branch']
            or pr.get('head', {}).get('repo', {}).get('id') != note['repo_id']
            or pr.get('base', {}).get('ref') != note['target_branch']
            or pr.get('base', {}).get('repo', {}).get('id') != note['repo_id']):
        raise AssignmentChanged('The approved pull request changed before work started')


def prepare_environment(directory, services=()):
    # The entrypoint uses Python -I: sibling modules are deliberately not on
    # sys.path. Load only this immutable, image-owned absolute path.
    build = runpy.run_path('/opt/forge/build_environment.py')
    env = build['environment'](directory, services)
    with (directory / '.git/info/exclude').open('a') as stream:
        stream.write('\n/.forge/\n/.home/\n')
    return env


# Kept in step with contracts.HEADLINE_LIMIT / PLAIN_LIMIT, which truncate what
# arrives. The image loads no sibling module, so the numbers live on both sides.
HEADLINE_LIMIT, PLAIN_LIMIT, OPERATOR_SUMMARY_LIMIT = 90, 360, 300
ENVIRONMENT_BLOCKERS = ('toolchain', 'sandbox', 'dependency_access', 'resources', 'ci_evidence', 'services', 'github_permission')
OPERATOR_ROLES = ('build', 'review', 'audit', 'recovery')


def role_properties(note):
    """Role-specific result fields: a fifth role is one line here."""
    if note['role']=='triage':return triage_properties(note)
    candidate = {'candidate': {'type': 'string', 'enum': [(note.get('candidate') or {}).get('key')]}}
    return {
        'build': {'issues': {'type': 'array', 'maxItems': 20, 'items': {'type': 'integer', 'minimum': 1},
                             'description': 'Same-repository prerequisite issue numbers for waiting_on_issues; empty otherwise.'},
                  'evidence': {'type': ['string', 'null'], 'maxLength': 2000,
                               'description': 'Exact quote from the assigned issue naming every prerequisite as #N; null otherwise.'},
                  'decision': {'anyOf': [{'type': 'null'}, {'type': 'object', 'properties': {
            **{k: {'type': 'string', 'minLength': 1, 'maxLength': 4000}
               for k in ('question', 'recommendation', 'evidence')},
            'options': {'type': 'array', 'minItems': 2, 'maxItems': 6,
                        'items': {'type': 'string', 'minLength': 1, 'maxLength': 1000}}},
            'required': ['question', 'options', 'recommendation', 'evidence'], 'additionalProperties': False}]}},
        'discussion': discussion_properties(note),
        'review': candidate,
        'audit': {**candidate, 'human_required': {'type': 'string', 'enum': ['required', 'not_required', 'unknown']},
                  'human_reason': {'type': 'string'}},
        'recovery': {'pattern': {'type': 'string'}, 'evidence': {'type': 'array', 'items': {'type': 'string'}},
                     'uncertainty': {'type': 'string'}, 'alternative': {'anyOf': [{'type': 'null'}, {
                         'type': 'object', 'properties': {k: {'type': 'string'} for k in ('change', 'reason', 'evidence')},
                         'required': ['change', 'reason', 'evidence'], 'additionalProperties': False}]}},
        'strategist': strategist_properties(),
    }.get(note['role'], {})


def discussion_properties(note):
    # Exactly the decisions Forge offers on this issue right now, Close included when it is closable.
    actions = [a for a in (note.get('context') or {}).get('actions') or [] if isinstance(a, str)]
    proposal = {'type': 'object', 'properties': {'action': {'type': 'string', 'enum': actions},
        'reason': {'type': 'string', 'maxLength': 2000}}, 'required': ['action', 'reason'], 'additionalProperties': False}
    return {'proposal': {'anyOf': [{'type': 'null'}, proposal]} if actions else {'type': 'null'},
            'close_now': {'type': 'boolean', 'description': "True only when the operator's latest message explicitly asks to close "
                          'this issue and proposal.action is close; Forge then closes it for them. Otherwise false.'}}


def triage_properties(note):
    # Supplied by the host from its canonical CLI planning action contract.
    row = note['context'].get('rows_schema')
    if not isinstance(row,dict) or not isinstance(row.get('anyOf'),list) or not row['anyOf']:
        raise PreparationError('Triage assignment requires its captured row schema')
    ref = note['context'].get('note_sources_schema')
    if not isinstance(ref,dict) or not isinstance(ref.get('anyOf'),list) or not ref['anyOf']:
        raise PreparationError('Triage assignment requires its captured note source schema')
    guidance=('Planning recommendations awaiting operator approval. Separate observed facts from proposed actions; '
              'never claim this planning run closed, approved, grouped or relabeled issues. Fixed work does not imply a closed GitHub issue.')
    memory={'type':'object','properties':{'content':{'type':'string','minLength':1,'maxLength':8192,'description':guidance},
        'sources':{'type':'array','minItems':1,'maxItems':20,'items':ref}},
        'required':['content','sources'],'additionalProperties':False}
    note={'type':'object','properties':{**memory['properties'],'note_id':{'type':['string','null']},
        'kind':{'type':'string','enum':['convention','decision','lesson','question','next_action'],
                'description':'Decision means a planning recommendation, not operator approval or an applied change.'}},
        'required':['note_id','kind','content','sources'],'additionalProperties':False}
    return {'summary':{'type':'string','description':guidance},
        'headline':{'type':'string','maxLength':HEADLINE_LIMIT,'description':guidance},
        'plain':{'type':'string','maxLength':PLAIN_LIMIT,'description':guidance},
        'rows':{'type':'array','items':row},'notes':{'type':'array','maxItems':5,'items':note},
        'handoff':{'anyOf':[{'type':'null'},memory]}}


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


def plan_schema(version):
    def object_schema(properties):
        return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}
    strings = {'type': 'array', 'items': {'type': 'string'}}
    key = {'type': 'string', 'pattern': '^[a-z][a-z0-9_-]{0,63}$'}
    scope = {'type': 'string', 'minLength': 1}
    if version == 1:
        return object_schema({'children': {'type': 'array', 'items': object_schema({
            'key': key, 'member_id': {'type': 'string'}, 'scope': scope, 'dependencies': strings})}})
    if version != 2:
        raise ValueError('Unsupported managed assignment manifest version')
    edges = {'type': 'array', 'items': object_schema({
        'prerequisite_member_id': {'type': 'string'}, 'member_id': {'type': 'string'}})}
    task = object_schema({'key': key, 'executor': {'type': 'string', 'enum': ['owner', 'child']},
        'member_ids': strings, 'scope': scope, 'depends_on': strings, 'internalizes': edges})
    return object_schema({'version': {'type': 'integer', 'enum': [2]}, 'retained_task_ids': strings,
        'tasks': {'type': 'array', 'items': task}, 'owner_final': object_schema({
            'member_ids': strings, 'scope': {'type': 'string'}, 'internalizes': edges})})


def schema(note):
    props = {'version': {'type': 'integer', 'enum': [1]},
             'attempt': {'type': 'string', 'enum': [note['id']]},
             'role': {'type': 'string', 'enum': [note['role']]},
             'outcome': {'type': 'string', 'enum': ['pass', 'reject', 'unable', 'wait', 'blocked_environment']},
             'blocker': {'type': ['string', 'null'], 'enum': [None, *ENVIRONMENT_BLOCKERS]},
             'summary': {'type': 'string'},
             # Plain language for a reader who does not read code, in every role.
             'headline': {'type': 'string', 'maxLength': HEADLINE_LIMIT},
             'plain': {'type': 'string', 'maxLength': PLAIN_LIMIT}}
    props.update(role_properties(note))
    if note['role'] in OPERATOR_ROLES:
        props['operator_summary'] = {'type': 'string', 'minLength': 1, 'maxLength': OPERATOR_SUMMARY_LIMIT}
    if note['role'] in ('build', 'review', 'audit', 'recovery'):
        props['outcome']['enum'].append('needs_human_verification')
        props['verification'] = {'anyOf': [{'type': 'null'}, {'type': 'object', 'properties': {
            'what': {'type': 'string', 'minLength': 1, 'maxLength': 4000},
            'why': {'type': 'string', 'minLength': 1, 'maxLength': 4000},
            'checks': {'type': 'array', 'maxItems': 20, 'items': {'type': 'string', 'minLength': 1, 'maxLength': 200}}},
            'required': ['what', 'why', 'checks'], 'additionalProperties': False}]}
        props['required_services'] = {'type': 'array', 'maxItems': 3,
                                      'items': {'type': 'string', 'enum': ['mongodb', 'postgres', 'redis']}}
    if note['role'] == 'build':
        props['outcome']['enum'].extend(['needs_decision', 'waiting_on_issues'])
        props['outcome']['description'] = ('Use waiting_on_issues for open prerequisite issues, not needs_decision '
                                          'or blocked_environment. dependency_access is for registry, repository or network access failures.')
    if note.get('builder_stage') or note.get('delegation') or note.get('task'):
        props['delegation_plan'] = {'anyOf': [{'type': 'null'},
            plan_schema(note.get('input_manifest', {}).get('manifest_version', 1))]}
        props['output_commit'] = {'type': ['string', 'null']}
        if note.get('builder_stage') == 'planning':
            props['outcome']['enum'] = list(PLANNING_OUTCOMES)
    return {'type': 'object', 'properties': props, 'required': list(props), 'additionalProperties': False}



def checkout_recovery(note, directory, git):
    """Investigate the failed checkout, never the repository's current default."""
    original = note['failure']['input_manifest']
    revision = original.get('head_oid') or original.get('base_oid')
    if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}(?:[0-9a-f]{24})?', revision):
        raise PreparationError('The failed assignment has no verified checkout revision')
    git('-C', str(directory), 'fetch', 'origin', revision)
    git('-C', str(directory), 'checkout', '--detach', revision)


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
    check_publication_target(note, env)
    git('clone', '--no-checkout', 'https://github.com/' + note['repo'] + '.git', str(directory))
    git('-C', str(directory), 'config', 'user.name', note['publisher'])
    git('-C', str(directory), 'config', 'user.email', note['publisher'] + '@users.noreply.github.com')
    selected = note.get('candidate')
    if note['role'] == 'recovery':
        checkout_recovery(note, directory, git)
    elif note['role'] == 'build' and note.get('resume_branch'):
        # A memory retry may have pushed newer work than the last accepted
        # candidate. Resume that branch, preserving the PR and published commits.
        ref = 'refs/heads/' + note['resume_branch']
        published = git('-C', str(directory), 'ls-remote', '--heads', 'origin', ref).stdout.strip()
        if published:
            if note.get('resume_head') and published.split()[0] != note['resume_head']:
                raise AssignmentChanged('The approved branch changed before work started')
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
    elif note.get('input_manifest', {}).get('base_oid'):
        git('-C', str(directory), 'fetch', 'origin', note['input_manifest']['base_oid'])
        git('-C', str(directory), 'checkout', '--detach', note['input_manifest']['base_oid'])
    elif note['role'] in ('strategist','triage'):
        # Read-only assignment: detach at the given head, never create a branch.
        git('-C', str(directory), 'fetch', 'origin', note['head'])
        git('-C', str(directory), 'checkout', '--detach', note['head'])
    else:
        git('-C', str(directory), 'checkout', '--detach', 'origin/HEAD')
    if note['role'] == 'build':
        git('-C', str(directory), 'checkout', '-B', note['branch'])
    inputs = note.get('bundle_inputs', [])
    for index, output in enumerate(inputs):
        bundle = '/work/input-' + str(index) + '.bundle'
        git('-C', str(directory), 'bundle', 'verify', bundle)
        git('-C', str(directory), 'fetch', bundle, 'HEAD')
        actual = git('-C', str(directory), 'rev-parse', 'FETCH_HEAD').stdout.strip()
        if actual != output['commit_oid']:
            raise PreparationError('Task output does not match the assigned revision')
    if note['role'] == 'recovery':
        existing = note['failure']['input_manifest'].get('existing_candidates', [])
    else:
        existing = note.get('input_manifest', {}).get('existing_candidates', []) if note.get('builder_stage') == 'integration' else []
    for source in existing:
        git('-C', str(directory), 'fetch', 'origin', source['head_oid'])
        if git('-C', str(directory), 'rev-parse', 'FETCH_HEAD').stdout.strip() != source['head_oid']:
            raise PreparationError('Existing candidate differs from its approved revision')
    merge_refs = [row['head_oid'] for row in existing] + [row['commit_oid'] for row in inputs]
    for index, commit in enumerate(merge_refs):
        # Conflicts belong to the owning builder/child implementation, not a
        # second integration worker. Keep the merge state for that assignment.
        merged = subprocess.run(['git', '-C', str(directory), 'merge', '--no-edit', commit],
                                env=env, capture_output=True, timeout=180)
        if merged.returncode:
            note['integration_conflicts'] = {'current': commit, 'remaining': merge_refs[index + 1:]}
            break
    env.update(prepare_environment(directory, (note.get('environment') or {}).get('services', [])))
    registry = runpy.run_path('/opt/forge/harnesses/registry.py')
    adapter = registry['adapter'](note.get('provider', 'codex'))
    contract = schema(note)
    begin_agent(env)
    code = adapter['run'](note, directory, env, contract)
    if (note.get('task') or note.get('delegation')) and Path('/work/result.json').is_file():
        # Export belongs to the completed implementation, not environment setup.
        # A bad claimed commit must charge a failed attempt rather than pause for
        # infrastructure that was already ready enough to run the provider.
        if not export_child(note, directory, inputs, git):
            return 1
    return code


def export_child(note, directory, inputs, git, *, result_path=Path('/work/result.json'), bundle_path='/work/output.bundle'):
    try:
        report = json.loads(result_path.read_text())
    except (ValueError, OSError):
        return False  # Controller classifies the retained malformed result.
    if not isinstance(report, dict) or report.get('outcome') not in {'pass', 'needs_human_verification'}:
        return True
    try:
        head = git('-C', str(directory), 'rev-parse', 'HEAD').stdout.strip()
        if report.get('output_commit') != head:
            raise PreparationError('Task result does not name its checkout head')
        base = note['input_manifest']['base_oid']
        git('-C', str(directory), 'merge-base', '--is-ancestor', base, head)
        for output in inputs:
            git('-C', str(directory), 'merge-base', '--is-ancestor', output['commit_oid'], head)
        git('-C', str(directory), 'bundle', 'create', bundle_path, 'HEAD', '^' + base)
    except PreparationError:
        report.update(outcome='unable', output_commit=None,
                      summary='Task output could not be verified and retained as the assigned commit.',
                      headline='The task output could not be retained',
                      plain='The implementation did not produce a verifiable commit containing its assigned inputs.')
        result_path.write_text(json.dumps(report))
        return False
    return True


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except AssignmentChanged as error:
        note = json.loads(Path(os.environ['FORGE_ASSIGNMENT']).read_text())
        report = {'version': 1, 'attempt': note['id'], 'role': 'build', 'outcome': 'needs_decision',
                  'summary': str(error), 'headline': 'The pull request changed before work started',
                  'plain': 'Forge kept the existing work unchanged. Review the new revision before resuming this delivery.',
                  'blocker': None, 'required_services': [], 'verification': None, 'issues': [], 'evidence': None,
                  'delegation_plan': None, 'output_commit': None,
                  'decision': {'question': 'Continue from the changed pull request?',
                               'options': ['Review and approve the current revision', 'Keep this delivery paused'],
                               'recommendation': 'Review the current revision before resuming', 'evidence': str(error)}}
        output = Path('/work/result.json.tmp'); output.write_text(json.dumps(report)); output.replace('/work/result.json')
        raise SystemExit(0)
    except (PreparationError, OSError) as error:
        note = json.loads(Path(os.environ['FORGE_ASSIGNMENT']).read_text())
        runpy.run_path('/opt/forge/build_environment.py')['blocked'](note, 'Forge could not prepare the disposable checkout before work started. Check repository access, revision availability and disk space.', 'dependency_access')
        raise SystemExit(125) from None
