"""Resumable installation-owned private repositories, using local GitHub authority."""
from .contracts import SetupError, require


def prepare_targets(state, save, github, *, apply=False):
    """Resolve both scratch repositories before App consent can be requested.

    A pending write is observed on every resume. Missing ambiguous writes are
    never repeated; their deterministic names can be inspected by an operator.
    """
    intent = state['intent']
    owner = intent['owner']
    saved = state.setdefault('smoke_targets', {})
    smoke = next((row['target'] for row in intent['repositories']
                  if row['target']['name'] == intent['smoke_target']), None)
    targets = {}
    for kind in ('smoke', 'publication'):
        if kind == 'smoke' and smoke:
            targets[kind] = {key: smoke[key] for key in ('id', 'name')}
            continue
        name = 'forge-' + kind + '-' + state['inputs_digest'][:12]
        full_name = owner['login'] + '/' + name
        marker = 'Forge installation ' + state['inputs_digest'] + ' ' + kind
        record = saved.get(kind)
        require(record is None or record['name'] == full_name and record['marker'] == marker,
                'smoke_target_changed', 'The saved installation target changed.')
        try:
            value = github._request('GET', '/repos/' + full_name)
        except SetupError as error:
            if error.code != 'github_not_found':
                raise
            value = None
        if value is None:
            if record and record.get('write_started'):
                return {'status': 'write_unconfirmed', 'repository': full_name,
                        'reason': 'The repository creation was sent but cannot be confirmed. Inspect its deterministic name before retrying.'}
            if not apply:
                return {'status': 'targets_pending', 'repository': full_name,
                        'reason': 'Apply creates a private installation-owned repository.'}
            record = saved[kind] = {'name': full_name, 'marker': marker, 'write_started': True}
            save()
            path = '/user/repos' if owner['type'] == 'User' else '/orgs/' + owner['login'] + '/repos'
            if owner['type'] == 'User':
                require(github.identity()['id'] == owner['id'], 'scratch_owner_required',
                        'Sign in locally as the selected personal repository owner.')
            github._request('POST', path, {'name': name, 'description': marker, 'private': True,
                                          'auto_init': True, 'has_issues': True})
            value = github._request('GET', '/repos/' + full_name)
        require(isinstance(value, dict) and type(value.get('id')) is int and value['id'] > 0
                and value.get('full_name', '').lower() == full_name.lower()
                and value.get('owner', {}).get('id') == owner['id'] and value.get('private') is True
                and value.get('description') == marker and not value.get('archived') and not value.get('disabled'),
                'scratch_identity_changed', 'The deterministic scratch name belongs to another repository or installation.')
        target = {'id': value['id'], 'name': value['full_name']}
        require(not record or not record.get('target') or record['target'] == target,
                'scratch_identity_changed', 'The scratch repository was replaced.')
        saved[kind] = {'name': full_name, 'marker': marker, 'write_started': True, 'target': target}
        save()
        targets[kind] = target
    all_targets = {row['target']['id']: {key: row['target'][key] for key in ('id', 'name')}
                   for row in intent['repositories']}
    all_targets.update({target['id']: target for target in targets.values()})
    return {'status': 'targets_ready', **targets, 'repositories': list(all_targets.values())}
