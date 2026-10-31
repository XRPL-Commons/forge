"""Public installation progress. Never send journals or credentials to the UI."""


def label(name, intent):
    labels = {'targets': 'Test repositories', 'host': 'Host installation', 'network': 'Private HTTPS',
              'consent': 'GitHub App approval', 'identity': 'Operator sign-in',
              'environment': 'Database test environment', 'smoke.prepare': 'First-merge test preparation',
              'smoke': 'First-merge test', 'handover': 'Repository handover'}
    if name in labels:
        return labels[name]
    kind, _, key = name.partition('.')
    target = next((item['target']['name'] for item in intent['repositories']
                   if str(item['target']['id']) == key), key)
    return {'account': 'Coding account', 'repository': 'Repository setup',
            'publication': 'Signed publication'}.get(kind, kind) + ': ' + target


def snapshot(state, now, *, runner_state='running', current=None):
    stages = []
    for name, row in state['stages'].items():
        receipt = row.get('receipt') or {}
        status = {'applying': 'running', 'complete': 'complete', 'pending': 'waiting',
                  'needs_user_action': 'failed' if receipt.get('error') else 'waiting'}[row['status']]
        if name == current:
            status = 'running'
        item = {'id': name, 'label': label(name, state['intent']), 'status': status,
                **{key: row[key] for key in ('started_at', 'completed_at') if key in row}}
        # Error messages and receipts can contain provider output, device codes,
        # signed URLs or other private data. Only curated public explanations leave.
        if status == 'failed':
            item['reason'] = ('The CLI could not confirm your operator sign-in. Open Forge and sign in to continue.'
                              if receipt.get('error') == 'identity_unverified' else
                              'This step stopped. Check the installer for details before retrying.')
        elif row['status'] == 'needs_user_action':
            item['reason'] = 'This step needs your attention in the installer.'
        stages.append(item)
    return {'version': 1, 'started_at': state['started_at'], 'updated_at': now,
            'runner': 'cli', 'state': runner_state, 'stages': stages}
