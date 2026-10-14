"""Typed administrative SSH setup surface for a newly installed factory.

No HTTP caller can invoke these operations. Repository preparation reuses the
same requirements and checks as scoped clients, before the first explicit Start.
"""
import argparse
import json
from pathlib import Path
import sys

from .installation_apps import RoleAppHandoff
from .onboarding import Onboarding
from .repository_requirements import describe
from .repository_setup import RepositorySetup
from .store.runtime import required
from forge_client.contracts import fields, repository, require


class InstallationSetup:
    def __init__(self, root, *, setup=None, handoff=None):
        self.root = Path(root)
        self.store = required(root)
        self.setup = setup or Onboarding(root)
        self.handoff = handoff or RoleAppHandoff(root, setup=self.setup)

    def call(self, request):
        fields(request, ('version', 'setup_id', 'actor_id', 'generation', 'action', 'parameters'))
        require(request['version'] == 1)
        self.handoff.operator(request['actor_id'])
        data = self.setup.read()
        action, params = request['action'], request['parameters']
        allowed = {'status': (), 'app': ('role', 'owner'), 'refresh': (), 'select': ('repository',),
                   'authorize': ('instance', 'repository', 'capability'), 'requirements': ('repository',),
                   'verify_repository': ('repository', 'revision'), 'check': ('revision',),
                   'start': ('revision',), 'account': ('provider', 'kind', 'secret'),
                   'pilot': ('repository', 'number', 'started_at')}
        require(action in allowed, 'invalid_action', 'Unknown installation setup action.')
        fields(params, allowed[action])
        if action != 'status':
            require(request['generation'] == data['generation'], 'setup_changed', 'Setup changed; inspect before continuing.')
        if action == 'pilot':
            from .factory.first_merge import proof
            return proof(self.root, **params)
        if action == 'status':
            return self.view()
        require(data['phase'] == 'setup' or action == 'account' and data['phase'] == 'active', 'setup_complete', 'Factory is active; use the scoped repository commands.')
        if action == 'app':
            return self.handoff.begin(setup_id=request['setup_id'], actor_id=request['actor_id'],
                                      generation=request['generation'], **params)
        if action == 'refresh':
            self.setup.refresh()
        elif action in ('authorize', 'requirements', 'verify_repository', 'select'):
            target = repository(params['repository'])
            settings = self.setup.settings()
            if action == 'authorize':
                require(params['instance'] == self.store.instance_id
                        and params['capability'] in ('repositories:read', 'repositories:prepare'),
                        'setup_changed', 'This preparation belongs to another installation.')
                return {'authorized': True}
            if action == 'verify_repository':
                require(params['revision'] == settings.view()['revision'], 'setup_changed', 'Repository setup changed.')
                require(settings.refresh(), 'setup_busy', 'Repository access refresh is already running.')
                view = describe(settings, target, initial_setup=True)
                if view['readiness']['discovered']:
                    require(RepositorySetup(settings).check(target['id'], params['revision']),
                            'setup_busy', 'Repository checks are already running.')
            if action == 'select':
                row = next((r for r in settings.view()['repositories'] if r['id'] == target['id']), None)
                require(row and row['name'].lower() == target['name'].lower(), 'repository_changed', 'Refresh repository access first.')
                if data.get('selected') != target:
                    self.setup.select(target['id'])
            else:
                return describe(settings, target, initial_setup=True)
        elif action == 'account':
            require(params['kind'] in ('api_key', 'token') and isinstance(params['secret'], str)
                    and 16 <= len(params['secret']) <= 16384, 'invalid_account', 'Enter the selected provider credential privately.')
            from .factory.control import controller_lock
            from .sqlite_identity import identity_effect_lock
            from .store.runtime import writable
            # Match terminal login serialization before changing an active runtime.
            with identity_effect_lock(self.root), controller_lock(self.root, timeout=120):
                self.handoff.operator(request['actor_id'])
                require(self.setup.read()['generation'] == request['generation'], 'setup_changed')
                self.setup.connect(params['provider'], **{params['kind']: params['secret']})
                with self.store.write() as db:
                    writable(db)
                    db.execute('DELETE FROM provider_cooldowns WHERE provider=?', (params['provider'],))
        else:
            self.setup.queue(action, params['revision'])
        return self.view()

    def view(self):
        view = self.setup.view()
        # No credentials, private challenge, transcripts or host commands leave this surface.
        return {'status': 'factory_active' if view['phase'] == 'active' else 'checking' if view['operation'] else 'setup_required',
                'instance': self.store.instance_id, 'ready_label': self.setup.runtime()['ready_label'], **{key: view[key] for key in
                    ('generation', 'revision', 'phase', 'roles', 'selected', 'checks', 'can_start', 'operation', 'error')},
                'harnesses': [{key: item[key] for key in ('id', 'name', 'connected', 'models')} for item in view['harnesses']]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(65537)
        require(len(raw) <= 65536)
        result = InstallationSetup(args.root).call(json.loads(raw))
        print(json.dumps({'ok': True, 'result': result}, allow_nan=False))
        return 0
    except Exception as error:
        from forge_client.contracts import SetupError
        code = error.code if isinstance(error, SetupError) else 'factory_setup_failed'
        print(json.dumps({'ok': False, 'error': code}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
