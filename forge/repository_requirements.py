"""Credential-free setup requirements shared by future operator transports.

This read does not grant access, write configuration, verify protection, or
expose an HTTP route. The authenticated caller must authorize the target before
calling it. Activation continues through RepositorySetup's existing gates.
"""
from forge_client.contracts import repository, requirements
from .repository_policy import github_protection


def describe(settings, target, *, initial_setup=False):
    repository(target)
    if settings.root.name == 'setup' and not initial_setup:
        raise ValueError('Finish initial Forge setup before adding repositories')
    if settings.paused():
        raise ValueError('Factory reset is in progress')
    view = settings.view()
    row = next((r for r in view['repositories'] if r['id'] == target['id']), None)
    if row and row['name'].lower() != target['name'].lower():
        raise ValueError('Repository identity changed; resolve it again')
    if any(r['name'].lower() == target['name'].lower() and r['id'] != target['id'] for r in view['repositories']):
        raise ValueError('Repository identity changed; resolve it again')
    roles = {}
    for role in ('build', 'review', 'audit', 'controller'):
        connection = view['config']['connections'].get(role) or {}
        ids = connection.get('installation_ids', [connection['installation_id']] if 'installation_id' in connection else [])
        roles[role] = {'app_id': connection.get('app_id'), 'installation_ids': ids,
                       'access': row['access'][role] if row else
                       ('missing_access' if view['roles'][role]['status'] == 'ok' else view['roles'][role]['status'])}
    from .factory.config import load as runtime_config, merge_check_scope
    check_scope=merge_check_scope(runtime_config(settings.authority.root,scope=settings.scope),target['id'])
    return requirements({
        'version': 1, 'repository': {'id': target['id'], 'name': row['name'] if row else target['name']},
        'revision': view['revision'], 'roles': roles,
        'readiness': {'discovered': row is not None, 'enabled': bool(row and row['enabled']),
                      'access_ready': bool(row and row['access_ready']),
                      'protection_ready': bool(row and row['protection_ready']),
                      'protection_status': row['protection_status'] if row else 'undiscovered'},
        'baseline': {'github_protection': github_protection(view['config'], target['id']), 'branch': (row.get('protection') or {}).get('branch') if row and row['protection_status'] in ('ready', 'needs_setup') else None,
                     'required_approvals': 2, 'dismiss_stale_reviews': True, 'merge_check_scope':check_scope},
    })
