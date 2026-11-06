"""Operator account recovery using the installation's owned device-login lifecycle."""
import time
import uuid

from forge_client.contracts import fields, require
from .installation_accounts import DeviceAccount
from .host_setup import LoginBusy
from .onboarding import Onboarding
from .settings import Settings
from .sqlite_identity import audit_in, identity_effect_lock
from .store.configuration import Configuration
from .store.runtime import required


class AccountWeb:
    def __init__(self, root, *, setup=None, clock=time.time, device=None):
        self.root, self.clock = root, clock
        self.setup = setup or Onboarding(root, clock=clock)
        self.store = required(root)
        self.configuration = Configuration(self.store, clock=clock)
        self.device = device or DeviceAccount(root, setup=self.setup, clock=clock)

    def saved(self, generation):
        return self.configuration.get('account_web', generation)

    def view(self):
        data = self.setup.read()
        saved = self.saved(data['generation'])
        record = data.get('device_logins', {}).get('codex')
        account = {'provider': 'codex', 'status': 'idle'}
        if record:
            try:
                result = self.device.call('codex', generation=data['generation'], setup_id=record['setup_id'])
                account = {key: result[key] for key in ('provider', 'status', 'url', 'code', 'expires_at') if key in result}
            except (ValueError, RuntimeError, OSError):
                account['status'] = 'account_unavailable'
        operation = saved['settings'] if saved else {}
        return {'generation': data['generation'], 'revision': saved['revision'] if saved else 0,
                'setup_revision': self.setup.revision(data), 'account': account,
                'operation': {key: operation[key] for key in ('status', 'updated_at', 'error', 'verification') if key in operation},
                'browser_providers': ['codex'], 'active': data['phase'] == 'active'}

    def request(self, value, *, actor_id):
        fields(value, ('generation', 'revision', 'setup_revision', 'provider', 'action'))
        require(value['provider'] == 'codex', 'device_login_unsupported')
        require(value['action'] in ('connect', 'reconnect', 'retry', 'verify'), 'invalid_action')
        with identity_effect_lock(self.store):
            data = self.setup.read(); generation = data['generation']
            require(generation == value['generation'] and data['phase'] == 'active', 'setup_changed')
            require(not Settings(self.root).paused(), 'factory_paused')
            saved = self.saved(generation)
            require(value['revision'] == (saved['revision'] if saved else 0)
                    and value['setup_revision'] == self.setup.revision(data), 'account_changed')
            record = data.get('device_logins', {}).get('codex')
            expired_retry = (saved and saved['settings']['status'] == 'connecting'
                             and value['action'] in ('retry', 'reconnect') and record
                             and self.clock() >= record['expires_at'])
            if saved and saved['settings']['status'] in ('queued', 'connecting', 'verifying') and not expired_retry:
                return self.view()
            require(not data.get('operation'), 'setup_busy')
            connected = next(row['connected'] for row in self.setup.harnesses(data) if row['id'] == 'codex')
            require(value['action'] != 'verify' or connected, 'account_not_connected')
            require(value['action'] != 'connect' or not connected, 'account_already_connected')
            record = data.get('device_logins', {}).get('codex')
            operation = {'status': 'queued', 'action': value['action'], 'updated_at': self.clock(),
                         'setup_id': record['setup_id'] if record else uuid.uuid4().hex,
                         'previous_session': record['id'] if record else None}
            with self.store.write() as db:
                self.configuration.replace_in(db, 'account_web', operation, subject=generation,
                                              expected_revision=saved['revision'] if saved else 0)
                audit_in(db, self.clock, 'account_operation_requested', details={
                    'actor_id': actor_id, 'provider': 'codex', 'action': value['action'], 'generation': generation})
        return self.view()

    def tick(self):
        with identity_effect_lock(self.store):
            data = self.setup.read(); generation = data['generation']; saved = self.saved(generation)
            if data['phase'] != 'active' or Settings(self.root).paused() or not saved:
                return
            operation = saved['settings']
            if operation['status'] not in ('queued', 'connecting', 'verifying'):
                return
            following = dict(operation)
            try:
                if operation['status'] == 'verifying':
                    if data.get('operation'): return
                    harness = next(row for row in self.setup.harnesses(data) if row['id'] == 'codex')
                    passed = harness['models'] and all(row['status'] == 'passed' for row in harness['models'])
                    following['status'] = 'complete' if passed else 'failed'
                    if not passed: following['error'] = 'Codex verification did not pass. Try again.'
                else:
                    if data.get('operation'): return
                    if operation['action'] == 'verify':
                        result = {'status': 'connected'}
                    else:
                        record = data.get('device_logins', {}).get('codex')
                        original = (record['id'] if record else None) == operation['previous_session']
                        restart = original and operation['action'] in ('retry', 'reconnect')
                        if restart and record and record['phase'] not in ('failed', 'complete') and self.clock() < record['expires_at']:
                            observed = self.device.call('codex', generation=generation, setup_id=operation['setup_id'])
                            restart = observed['status'] == 'account_login_failed'
                        result = self.device.call('codex', generation=generation, setup_id=operation['setup_id'], apply=True,
                            retry=restart, reconnect=restart and operation['action'] == 'reconnect')
                    if result['status'] == 'connected':
                        self.setup.queue('harness_check', self.setup.revision(self.setup.read()), harness='codex')
                        following.update(status='verifying', verification=True)
                    else:
                        following['status'] = 'failed' if result['status'] in ('account_login_failed', 'write_unconfirmed') else 'connecting'
            except LoginBusy:
                return
            except (ValueError, RuntimeError, OSError):
                following.update(status='failed', error='Account connection stopped. Retry or check the factory status.')
            if following != operation:
                following['updated_at'] = self.clock()
                self.configuration.replace('account_web', following, subject=generation, expected_revision=saved['revision'])
