"""Scoped agent access to the same repository services used by the operator UI."""
from copy import deepcopy
import json
import re
import threading

from .client_connections import fields, need, ConnectionError
from .repository_requirements import describe
from .repository_setup import RepositorySetup
from .sqlite_identity import active_in, audit_in
from .store.transitions import _json
from .store.runtime import required
from forge_client.contracts import REVISION, positive

OPERATIONS = {'verify': 'repositories:prepare', 'enable': 'repositories:activate',
              'disable': 'repositories:activate'}


class ClientRepositories:
    def __init__(self, settings, clients, *, store=None, setup=None, launch=None):
        self.settings, self.clients = settings, clients
        store = store if store is not None else required(settings.root)
        self.sqlite_store, self.store = store, store
        self.setup = setup or RepositorySetup(settings)
        self.launch = launch or (lambda fn: threading.Thread(target=fn, daemon=True).start())

    def authorize(self, token, value, capability):
        return self.clients.authorize(token, value['repository'], capability, value['instance'], value['generation'])

    def read(self, token, value):
        fields(value, ('repository', 'instance', 'generation', 'section', 'number', 'page') +
               (('after',) + (('task_after',) if 'task_after' in value else ())
                if isinstance(value,dict) and value.get('section') == 'delivery' else ()))
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
            task_after = value.get('task_after', '')
            need(isinstance(task_after,str) and len(task_after) <= 400)
            service = public_views(self.settings.root)
            need(service is not None, 'runtime_unavailable', 'Delivery history requires the active SQLite runtime.')
            from .store.models import DeliveryPlanChanged
            try:
                result = service.delivery(f"{target['id']}:{value['number']}",after=value['after'],task_after=task_after)
            except DeliveryPlanChanged:
                raise ConnectionError('delivery_plan_changed','The delivery plan changed. Restart task paging.',409) from None
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

    @staticmethod
    def _record(row):
        value = dict(request=json.loads(row['payload_json']), status='complete' if row['status'] == 'completed' else row['status'],
                     created_at=row['created_at'])
        for source, target in [('finished_at', 'finished_at'), ('error_code', 'error'), ('expected_revision', 'expected_revision')]:
            if row[source] is not None:
                value[target] = row[source]
        if row['result_json'] is not None:
            value['result'] = json.loads(row['result_json'])
        return value

    @staticmethod
    def public(record):
        return {k: deepcopy(record[k]) for k in ('status', 'created_at', 'finished_at', 'error', 'result') if k in record} | {
            'id': record['request']['id'], 'action': record['request']['action'],
            'repository': deepcopy(record['request']['repository'])}

    def _get(self, identifier):
        with self.sqlite_store.read_snapshot() as db:
            active_in(db)
            row = db.execute('SELECT * FROM client_operations WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise KeyError(identifier)
            return self._record(row)

    def operation(self, token, value):
        fields(value, ('repository', 'instance', 'generation', 'id', 'action', 'revision'))
        need(isinstance(value['id'], str) and re.fullmatch(r'[a-f0-9]{32}', value['id']))
        need(isinstance(value['action'], str) and value['action'] in OPERATIONS)
        need(isinstance(value['revision'], str) and REVISION.fullmatch(value['revision']))
        grant = self.authorize(token, value, OPERATIONS[value['action']])
        binding = {**deepcopy(value), 'client': grant['id'], 'operator': grant['operator']}
        with self.sqlite_store.write() as db:
            active_in(db)
            row = db.execute('SELECT * FROM client_operations WHERE id=?', (value['id'],)).fetchone()
            if row:
                need(json.loads(row['payload_json']) == binding, 'operation_conflict',
                     'This operation ID belongs to a different request.', 409)
            else:
                need(db.execute('SELECT count(*) FROM client_operations').fetchone()[0] < 10000,
                     'operation_capacity', 'Operation history needs maintenance.', 409)
                repository = db.execute('SELECT id,full_name FROM repositories WHERE id=?', (value['repository']['id'],)).fetchone()
                if repository:
                    need(repository['full_name'].lower() == value['repository']['name'].lower(),
                         'scope_changed', 'Repository identity changed.', 403)
                # An unconfigured target stays in the typed request until the
                # repository service admits it; do not enable it by inserting a
                # lifecycle repository row as a side effect of requesting setup.
                db.execute('INSERT INTO client_operations(id,request_id,connection_id,repository_id,kind,status,payload_json,created_at,updated_at) '
                           "VALUES(?,?,?,?,?,'pending',?,?,?)", (value['id'], value['id'], grant['id'],
                            repository['id'] if repository else None, value['action'], _json(binding), self.settings.clock(), self.settings.clock()))
                from .mutation_audit import current, MutationAudit
                audit = current() or MutationAudit(self.sqlite_store, '/api/client/operation', clock=self.settings.clock)
                audit.bind(grant=grant)
                audit.describe(value)
                audit.details['command_request_id'] = value['id']
                audit.finish_in(db, 'accepted', 'accepted')
                row = db.execute('SELECT * FROM client_operations WHERE id=?', (value['id'],)).fetchone()
            result = self.public(self._record(row))
        if result['status'] == 'pending':
            self.launch(lambda: self.run(token, value))
        return result

    def update(self, identifier, **values):
        if set(values) - {'status', 'error', 'result', 'finished_at', 'expected_revision'}:
            raise ValueError('Unsupported client operation field')
        mapping = dict(status='status', error='error_code', result='result_json', finished_at='finished_at', expected_revision='expected_revision')
        with self.sqlite_store.write() as db:
            active_in(db)
            row = db.execute('SELECT * FROM client_operations WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise KeyError(identifier)
            if row['status'] in {'completed', 'failed'}:
                return  # Completion and failure receipts cannot be overwritten.
            parameters, columns = [], []
            for key, value in values.items():
                if key == 'status':
                    need(value in {'pending', 'complete', 'failed'})
                    value = 'completed' if value == 'complete' else value
                elif key == 'result':
                    value = _json(value)
                columns.append(mapping[key] + '=?')
                parameters.append(value)
            db.execute('UPDATE client_operations SET ' + ','.join(columns + ['updated_at=?']) + ' WHERE id=?',
                       (*parameters, self.settings.clock(), identifier))
            if values.get('status') in {'complete', 'failed'}:
                audit_in(db, self.settings.clock, 'client_repository_' + row['kind'], connection=row['connection_id'],
                         request_id=identifier, outcome=values['status'], details={'kind': 'operation_effect', 'error_code': values.get('error')})

    def run(self, token, value):
        from .mutation_audit import MutationAudit, request_context
        binding = self._get(value['id'])['request']
        audit = MutationAudit(self.sqlite_store, '/api/client/operation', clock=self.settings.clock)
        audit.bind(grant={'operator': binding['operator'], 'id': binding['client']})
        audit.describe(value)
        with request_context(audit):
            return self._run(token, value)

    def _run(self, token, value):
        from .repository_requirements import describe
        from .settings import digest as config_digest
        identifier, action, target = value['id'], value['action'], value['repository']
        try:
            with self.settings.locked('client-' + identifier, blocking=False):
                record = self._get(identifier)
                if record['status'] != 'pending':
                    return
                self.authorize(token, value, OPERATIONS[action])
                current = describe(self.settings, target)
                if action != 'verify' and record.get('expected_revision') == current['revision']:
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
                    row = next((row for row in config['repositories'] if row['id'] == target['id']), None)
                    if row is None:
                        config['repositories'].append({**target, 'enabled': action == 'enable'})
                    else:
                        row['enabled'] = action == 'enable'
                    self.update(identifier, expected_revision=config_digest(config))
                    with self.clients.effect_guard(token, value['instance'], value['generation']):
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
