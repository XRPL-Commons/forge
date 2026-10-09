"""Scoped agent access to the same repository services used by the operator UI."""
from copy import deepcopy
import re
import threading

from .client_connections import fields, need, ConnectionError
from .repository_requirements import describe
from .repository_setup import RepositorySetup
from .settings import digest
from .state import AtomicStore
from forge_client.contracts import REVISION, positive

OPERATIONS = {'verify': 'repositories:prepare', 'enable': 'repositories:activate',
              'disable': 'repositories:activate'}


class ClientRepositories:
    def __new__(cls, settings, clients, *, store=None, **kwargs):
        if cls is ClientRepositories:
            from .store.runtime import selected
            chosen = store if store is not None else selected(settings.root)
            if chosen is not None:
                from .sqlite_identity import SqliteClientRepositories
                return SqliteClientRepositories(settings, clients, store=chosen, **kwargs)
        return super().__new__(cls)

    def __init__(self, settings, clients, *, setup=None, launch=None):
        self.settings, self.clients = settings, clients
        self.setup = setup or RepositorySetup(settings)
        self.store = AtomicStore(settings.root / 'state/client-operations', 'operations')
        self.launch = launch or (lambda fn: threading.Thread(target=fn, daemon=True).start())

    def authorize(self, token, value, capability):
        return self.clients.authorize(token, value['repository'], capability,
                                      value['instance'], value['generation'])

    def read(self, token, value):
        fields(value, ('repository', 'instance', 'generation', 'section', 'number', 'page') + (('after',) if isinstance(value,dict) and value.get('section') == 'delivery' else ()))
        self.authorize(token, value, 'repositories:read')
        section = value['section']
        need(section in ('repository', 'activity', 'issues', 'issue', 'delivery'))
        need(type(value['page']) is int and 1 <= value['page'] <= 10000)
        need(positive(value['number']) if section in ('issue','delivery') else value['number'] is None)
        target = value['repository']
        requirements = describe(self.settings, target)
        if section == 'repository':
            rows = self.settings.view()['repositories']
            result = {'requirements': requirements,
                      'repository': next((r for r in rows if r['id'] == target['id']), None),
                      'connections': self.settings.view()['roles']}
        elif section == 'delivery':
            from .factory.control import public_views
            need(isinstance(value['after'],str) and len(value['after']) <= 200)
            service = public_views(self.settings.root)
            need(service is not None, 'runtime_unavailable', 'Delivery history requires the active SQLite runtime.')
            result = service.delivery(f"{target['id']}:{value['number']}",after=value['after'])
            need(result['repo_id'] == target['id'], 'repository_mismatch', 'Delivery belongs to another repository.')
        elif section == 'activity':
            from .factory.control import view
            state = view(self.settings.root)
            # Global overview, actions, slots and deployments can identify other
            # repositories. Return only health timing and this target's state.
            observation = state['observation']
            at = observation.get('at')
            fresh = type(at) in (int, float) and 0 <= self.settings.clock() - at <= max(120, state['runtime'].get('cycle_seconds', 30) * 3)
            result = {'active': state['active'], 'observed_at': observation.get('at'),
                      'fresh': fresh, 'healthy': not bool(observation.get('errors')) if fresh else None,
                      'enabled': state['runtime'].get('enabled', False),
                      'merge_enabled': state['runtime'].get('merge_enabled', False),
                      'issues': [r for r in state['issues'] if r['repo_id'] == target['id']],
                      'jobs': [r for r in state['jobs'] if r['repo_id'] == target['id']]}
        else:
            from .factory.issues import IssueInventory
            key = f"{target['id']}:{value['number']}" if section == 'issue' else None
            result = IssueInventory(self.settings.root).view(repo_id=target['id'], state='all',
                                                            page=value['page'], key=key)
            result['repositories'] = [r for r in result['repositories'] if r['id'] == target['id']]
            result['actions'] = [r for r in result['actions']
                                 if str(r.get('key', '')).startswith(str(target['id']) + ':')]
        self.authorize(token, value, 'repositories:read')
        return result

    def operation(self, token, value):
        fields(value, ('repository', 'instance', 'generation', 'id', 'action', 'revision'))
        need(isinstance(value['id'], str) and re.fullmatch(r'[a-f0-9]{32}', value['id']))
        need(isinstance(value['action'], str) and value['action'] in OPERATIONS)
        need(isinstance(value['revision'], str) and REVISION.fullmatch(value['revision']))
        grant = self.authorize(token, value, OPERATIONS[value['action']])
        binding = {**deepcopy(value), 'client': grant['id'], 'operator': grant['operator']}
        with self.store.transaction() as records:
            record = records.get(value['id'])
            if record:
                need(record['request'] == binding, 'operation_conflict',
                     'This operation ID belongs to a different request.', 409)
            else:
                need(len(records) < 10000, 'operation_capacity', 'Operation history needs maintenance.', 409)
                record = {'request': binding, 'status': 'pending', 'created_at': self.settings.clock()}
                records[value['id']] = record
            result = self.public(record)
        if result['status'] == 'pending':
            # The credential exists only in this process, never in the record.
            # A restart resumes only when an authorized client polls again.
            self.launch(lambda: self.run(token, value))
        return result

    @staticmethod
    def public(record):
        return {k: deepcopy(record[k]) for k in ('status', 'created_at', 'finished_at', 'error', 'result') if k in record} | {
            'id': record['request']['id'], 'action': record['request']['action'],
            'repository': deepcopy(record['request']['repository'])}

    def update(self, identifier, **values):
        with self.store.transaction() as records:
            records[identifier].update(values)

    def run(self, token, value):
        identifier, action, target = value['id'], value['action'], value['repository']
        try:
            # flock prevents duplicate execution across threads and processes;
            # process death releases it, leaving a resumable pending record.
            with self.settings.locked('client-' + identifier, blocking=False):
                record = self.store.read()[identifier]
                if record['status'] != 'pending':
                    return
                self.authorize(token, value, OPERATIONS[action])
                current = describe(self.settings, target)
                if action != 'verify' and record.get('expected_revision') == current['revision']:
                    # Config write succeeded but its response/receipt was lost.
                    self.finish(identifier, current)
                    return
                need(current['revision'] == value['revision'], 'revision_changed',
                     'Forge configuration changed. Inspect and create a new operation.', 409)
                if action in ('verify', 'enable'):
                    need(self.settings.refresh(), 'verification_busy', 'Access refresh is busy. Retry verification.', 409)
                    self.authorize(token, value, OPERATIONS[action])
                    current = describe(self.settings, target)
                    if current['readiness']['discovered']:
                        need(self.setup.check(target['id'], value['revision']), 'verification_busy',
                             'Repository verification is busy. Retry verification.', 409)
                self.authorize(token, value, OPERATIONS[action])
                current = describe(self.settings, target)
                need(current['revision'] == value['revision'], 'revision_changed',
                     'Forge configuration changed. Inspect and create a new operation.', 409)
                if action != 'verify':
                    if action == 'enable':
                        need(current['readiness']['access_ready'] and current['readiness']['protection_ready'],
                             'repository_not_ready', 'Refresh repository setup and repair the reported requirements.', 409)
                    config = deepcopy(self.settings.load())
                    row = next((r for r in config['repositories'] if r['id'] == target['id']), None)
                    if row is None:
                        config['repositories'].append({**target, 'enabled': action == 'enable'})
                    else:
                        row['enabled'] = action == 'enable'
                    self.update(identifier, expected_revision=digest(config))
                    # Serialize revocation against the actual configuration
                    # effect. Identity/scopes were checked immediately above.
                    with self.clients.store.transaction() as data:
                        context = self.clients._context(data, self.clients.generation())
                        self.clients._bearer(data, token, context)
                        need(context['instance'] == value['instance'] and context['generation'] == value['generation'],
                             'connection_changed', 'Connect to the current Forge instance again.', 409)
                        getattr(self.setup, action)(target['id'], value['revision'])
                self.authorize(token, value, OPERATIONS[action])
                self.finish(identifier, describe(self.settings, target))
        except BlockingIOError:
            return
        except ConnectionError as exc:
            self.update(identifier, status='failed', error=exc.code, finished_at=self.settings.clock())
        except (ValueError, OSError, RuntimeError, KeyError, TypeError):
            self.update(identifier, status='failed', error='verification_failed', finished_at=self.settings.clock())

    def finish(self, identifier, result):
        self.update(identifier, status='complete', result=result, finished_at=self.settings.clock())
