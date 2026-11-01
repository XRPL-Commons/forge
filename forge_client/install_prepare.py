"""Hand approved, credential-free preparation to the server."""
from copy import deepcopy
from .contracts import digest, require


def plan(state, targets, report):
    selected = state['intent']
    identity = lambda target: {key: target[key] for key in ('id', 'name')}
    return {'intent_digest': state['inputs_digest'], 'started_at': state['started_at'],
            'repositories': [{**row, 'target': identity(row['target']),
                              'github_protection': row.get('github_protection', 'required')}
                             for row in selected['repositories']],
            'smoke_repository': identity(targets['smoke']), 'smoke_issue': selected.get('smoke_issue'),
            'publication_repository': identity(targets['publication']),
            'environment': 'full' if any(row['env_tier'] == 'full' for row in report['repositories']) else 'standard',
            'providers': deepcopy(selected['providers'])}


def observe(state, factory, value):
    """Bind server progress before recording it, including a newly created issue."""
    require(isinstance(value, dict) and type(value.get('available')) is bool, 'invalid_host_response')
    if not value['available']:
        return value
    require(value.get('status') in ('preparation_pending', 'preparation_needs_operator', 'preparation_ready')
            and type(value.get('ready')) is bool
            and value['ready'] == (value['status'] == 'preparation_ready'), 'invalid_host_response')
    expected = state['server_preparation']['plan']
    require(value.get('setup_id') == factory.state['intent']['id']
            and value.get('generation') == factory.state['factory_binding']['generation']
            and value.get('intent_digest') == expected['intent_digest']
            and value.get('started_at') == expected['started_at']
            and value.get('repositories') == expected['repositories']
            and value.get('smoke_repository') == expected['smoke_repository'], 'preparation_changed')
    smoke = value.get('smoke')
    if smoke:
        selected = next((r for r in expected['repositories'] if r['target'] == expected['smoke_repository']), None)
        require(smoke.get('repository') == expected['smoke_repository']
                and type(smoke.get('number')) is int and smoke['number'] > 0
                and (expected['smoke_issue'] is None or smoke['number'] == expected['smoke_issue'])
                and smoke.get('smoke_policy') == (selected['merge_policy'] if selected else 'auto'), 'smoke_changed')
        require(not state.get('smoke') or state['smoke'] == smoke, 'smoke_changed')
        state['smoke'] = deepcopy(smoke)
    return value


def record(state, value, now):
    row = state['stages'].setdefault('preparation', {
        'inputs_digest': digest({'intent': state['inputs_digest'], 'stage': 'preparation'}), 'started_at': now})
    row.update(status='complete' if value.get('ready') else 'pending', receipt=deepcopy(value), uncertain_write=False)
    if value.get('ready'): row.setdefault('completed_at', now)
