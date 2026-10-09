"""Operator-authenticated browser handoff initiated by administrative setup CLI.

GitHub registration stays in the operator's browser. Only the role App's own
credential reaches Forge, through the existing manifest conversion path.
"""
from copy import deepcopy
import hashlib
import hmac
import html
import json
import re
import secrets
import time
from urllib.parse import parse_qs, urlsplit

from .onboarding import Onboarding, ROLES
from .operator_auth import OperatorAuth
from .store.runtime import selected


class RoleAppHandoff:
    def __init__(self, root, *, setup=None, auth=None, clock=time.time):
        self.setup = setup or Onboarding(root)
        self.auth = auth or OperatorAuth(root)
        self.store = selected(root)
        if self.store is None:
            raise ValueError('Fresh setup requires the selected SQLite runtime')
        self.clock = clock

    def operator(self, actor_id):
        config = self.auth.config()
        if type(actor_id) is not int or not config or actor_id not in config['operators']:
            raise ValueError('Use a configured Forge operator account')
        return config

    def begin(self, *, setup_id, actor_id, owner, role, generation):
        config = self.operator(actor_id)
        if (not isinstance(setup_id, str) or not re.fullmatch(r'[a-f0-9]{32}', setup_id)
                or role not in ROLES or not isinstance(owner, dict)
                or set(owner) != {'id', 'login', 'type'} or type(owner['id']) is not int or owner['id'] <= 0
                or owner['type'] not in ('User', 'Organization')
                or not re.fullmatch(r'[A-Za-z0-9-]{1,39}', owner.get('login', ''))):
            raise ValueError('Choose the setup, role and verified GitHub owner')
        verified = self.setup.api().request('GET', '/users/' + owner['login'], None)
        if (verified.get('id') != owner['id'] or verified.get('type') != owner['type']
                or verified.get('login', '').lower() != owner['login'].lower()):
            raise ValueError('GitHub owner identity changed; inspect before continuing')
        current = self.setup.read()
        if current['generation'] != generation:
            raise ValueError('Setup changed; inspect before continuing')
        if role in current['apps']:
            return {'status': 'app_registered', 'role': role, 'install_url': current['apps'][role]['install_url']}
        existing = current.get('cli_app_handoff')
        binding = dict(setup_id=setup_id, actor_id=actor_id, owner=owner, role=role,
                       generation=generation, instance=self.store.instance_id, origin=config['origin'])
        if (existing and existing['binding'] == binding and existing['expires'] > self.clock()
                and (current.get('challenge') or {}).get('state') == existing['state_hash']):
            return self._public(existing)
        browser = secrets.token_urlsafe(32)
        registration = self.setup.begin(role, owner['login'], 'organization' if owner['type'] == 'Organization' else 'personal',
                                        config['origin'], browser)
        state = parse_qs(urlsplit(registration['action']).query)['state'][0]
        registration['manifest']['redirect_url'] = config['origin'] + '/onboarding/cli/github/callback'
        handoff = {'binding': binding, 'state': state, 'state_hash': hashlib.sha256(state.encode()).hexdigest(),
                   'browser': browser, 'expires': self.clock() + 600, 'registration': registration}
        with self.setup.store.transaction() as data:
            if data['generation'] != generation or (data.get('challenge') or {}).get('state') != handoff['state_hash']:
                raise ValueError('Setup changed while registration was prepared')
            data['cli_app_handoff'] = handoff
        return self._public(handoff)

    @staticmethod
    def _public(handoff):
        return {'status': 'app_registration_required', 'role': handoff['binding']['role'],
                'expires_at': handoff['expires'],
                'url': handoff['binding']['origin'] + '/onboarding/cli/github/start?state=' + handoff['state']}

    def _bound(self, state, actor_id):
        config = self.operator(actor_id)
        data = self.setup.read()
        handoff = data.get('cli_app_handoff') or {}
        binding = handoff.get('binding') or {}
        state_hash = hashlib.sha256(state.encode()).hexdigest() if isinstance(state, str) else ''
        if (not hmac.compare_digest(handoff.get('state_hash', ''), state_hash)
                or handoff.get('expires', 0) <= self.clock()
                or binding.get('actor_id') != actor_id or binding.get('generation') != data['generation']
                or binding.get('instance') != self.store.instance_id or binding.get('origin') != config['origin']
                or (data.get('challenge') or {}).get('state') != state_hash):
            raise ValueError('Registration expired or belongs to another setup or operator; resume the CLI')
        self.setup.editable(data)
        return deepcopy(handoff)

    def page(self, state, actor_id):
        handoff = self._bound(state, actor_id)
        registration = handoff['registration']
        escape = html.escape
        # No script or credential appears in this page. Submission goes directly to GitHub.
        return ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                '<title>Register a Forge GitHub App</title><body><main><h1>Register the '
                + escape(handoff['binding']['role'].title()) + ' App</h1><p>GitHub owner: '
                + escape(handoff['binding']['owner']['login']) + '. Approve registration on GitHub, then install this App '
                'on the repository you will use. Return to the CLI afterward.</p><form method="post" action="'
                + escape(registration['action'], quote=True) + '"><input type="hidden" name="manifest" value="'
                + escape(json.dumps(registration['manifest']), quote=True)
                + '"><button type="submit">Continue on GitHub</button></form></main></body></html>')

    def callback(self, state, code, actor_id):
        handoff = self._bound(state, actor_id)
        target = self.setup.callback(state, code, handoff['browser'], owner_id=handoff['binding']['owner']['id'])
        with self.setup.store.transaction() as data:
            current = data.get('cli_app_handoff') or {}
            if current.get('state_hash') == handoff['state_hash']:
                data.pop('cli_app_handoff', None)
        return target
