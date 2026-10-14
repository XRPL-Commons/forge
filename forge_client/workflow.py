"""Resumable local coordination. Journals contain only validated setup metadata."""
import json
import os
from pathlib import Path
import tempfile
import uuid

from .contracts import SetupError, connection_identity, digest, require, requirements
from .preparation import Preparation


class Journal:
    def __init__(self, key, target, *, root=None):
        self.root = Path(root) if root else Path.home() / '.local/state/forge-client'
        require(not self.root.is_symlink(), 'invalid_storage', 'Client state must be a private directory.')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.root.stat()
        require(info.st_uid == os.getuid() and not info.st_mode & 0o077, 'invalid_storage', 'Client state must be private.')
        self.path = self.root / (digest({'connection': key, 'repository': target}) + '.json')

    def read(self):
        require(not self.path.is_symlink(), 'invalid_storage', 'Client journals must be regular private files.')
        if not self.path.exists():
            return None
        info = self.path.stat()
        require(info.st_uid == os.getuid() and not info.st_mode & 0o077 and info.st_size <= 1048576,
                'invalid_storage', 'Client journal is not a private bounded file.')
        try:
            value = json.loads(self.path.read_text())
            require(isinstance(value, dict) and value.get('version') == 1)
            return value
        except (ValueError, OSError):
            raise SetupError('invalid_journal', 'Client journal is unreadable. Inspect it before starting a new setup.') from None

    def save(self, value):
        fd, name = tempfile.mkstemp(dir=self.root, prefix='.pending-')
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(value, stream, allow_nan=False, sort_keys=True)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(name, self.path)
            directory = os.open(self.root, os.O_RDONLY)
            try: os.fsync(directory)
            finally: os.close(directory)
        finally:
            if os.path.exists(name): os.unlink(name)


class Pending(SetupError):
    pass


class Workflow:
    def __init__(self, client, github, target, *, journal=None, wait=120):
        require(type(wait) is int and 0 <= wait <= 600)
        self.client, self.github, self.target, self.wait = client, github, target, wait
        self.journal = journal or Journal(client.key, target)
        self.state = None

    def authorize(self, *args):
        return self.client.authorize(*args)

    def requirements(self, target):
        return self.client.requirements(target)

    def operation(self, action, target, revision):
        request = self.state.get('operation')
        if request:
            require(request['action'] == action and request['repository'] == target,
                    'operation_pending', 'Resume the recorded operation before starting another action.')
        else:
            request = {'id': uuid.uuid4().hex, 'action': action, 'repository': target, 'revision': revision}
            self.state['operation'] = request
            self.journal.save(self.state)
        deadline = self.client.clock() + self.wait
        while True:
            result = self.client.request('operation', target, **{k: request[k] for k in ('id', 'action', 'revision')})
            require(set(result) <= {'id', 'action', 'repository', 'status', 'created_at', 'finished_at', 'error', 'result'})
            require(result.get('id') == request['id'] and result.get('action') == action and result.get('repository') == target)
            require(result.get('status') in ('pending', 'complete', 'failed'))
            if result['status'] == 'complete':
                observed = requirements(result['result'])
                require(observed['repository'] == target)
                self.state['operation'] = None
                self.state['last_operation'] = request['id']
                self.journal.save(self.state)
                return observed
            if result['status'] == 'failed':
                allowed = {'revision_changed', 'repository_not_ready', 'verification_busy', 'verification_failed',
                           'invalid_token', 'insufficient_scope', 'connection_changed', 'setup_required'}
                code = result.get('error') if result.get('error') in allowed else 'verification_failed'
                self.state['operation'] = None
                self.journal.save(self.state)
                raise SetupError(code, 'Inspect repository readiness, then resume setup with the same requested options.')
            if self.client.clock() >= deadline:
                raise Pending('operation_pending', 'Run the same command with --resume to continue this operation.')
            self.client.sleep(min(2, max(0, deadline - self.client.clock())))

    def verify(self, target, revision):
        return self.operation('verify', target, revision)

    def enable(self, target, revision):
        return self.operation('enable', target, revision)

    def progress(self, phase, step):
        self.state['local_write'] = step if phase == 'started' else None
        if phase == 'completed':
            self.state['completed'].append(step)
        self.journal.save(self.state)

    def execute(self, action, *, apply=False, enable=False, resume=False):
        connection = self.client.status()
        observed = self.requirements(self.target)
        saved = self.journal.read()
        binding = {'instance': connection['instance'], 'generation': connection['generation'],
                   'client': connection['id'], 'target': self.target,
                   'apps': connection_identity(observed), 'action': action, 'enable': enable}
        if resume:
            require(saved is not None, 'no_operation', 'Start repository setup before requesting --resume.')
            require(saved['binding'] == binding, 'connection_changed',
                    'Setup identity, App configuration or requested action changed. Inspect before starting a new setup.')
            self.state = saved
        else:
            require(not saved or not (saved.get('operation') or saved.get('local_write')),
                    'operation_pending', 'Resume the previous setup before starting a new operation.')
            self.state = {'version': 1, 'id': uuid.uuid4().hex, 'binding': binding,
                          'operation': None, 'local_write': None, 'completed': []}
        preparation = Preparation(self.github, self, instance=binding['instance'], generation=binding['generation'],
                                  apps=binding['apps'], progress=self.progress)
        if action == 'setup' and not apply:
            require(not resume, 'apply_required', 'Resume setup with --apply and the original --enable intent.')
            return preparation.setup(self.target['name'])
        self.journal.save(self.state)
        try:
            pending = self.state.get('operation')
            if pending:
                self.operation(pending['action'], self.target, pending['revision'])
            uncertain = self.state.get('local_write')
            # First refresh independent evidence after a crash/browser handoff.
            # Verification is read-only on GitHub and is safe to repeat.
            if action in ('setup', 'verify'):
                observed = self.verify(self.target, self.requirements(self.target)['revision'])
            if uncertain:
                if uncertain['kind'] in ('github.ruleset.repair_bypass', 'github.classic.repair_bypass'):
                    from .rules import confirmed
                    if not confirmed(self.github, self.github.repository(self.target['name']), uncertain):
                        return self.result({'status': 'write_unconfirmed', 'next_actions': [{'kind': 'inspect_previous_bypass_write'}]})
                if uncertain['kind'] == 'github.ruleset.ensure_baseline' and not observed['readiness']['protection_ready']:
                    return self.result({'status': 'write_unconfirmed', 'next_actions': [{
                        'kind': 'open_url', 'url': 'https://github.com/' + self.target['name'] + '/settings/rules',
                        'reason': 'Inspect the previous rule write and repair it before resuming; Forge will not create a duplicate.'}]})
                self.state['local_write'] = None
                self.journal.save(self.state)
            if action == 'setup':
                result = preparation.setup(self.target['name'], apply=True, enable=enable)
            elif action == 'verify':
                readiness = observed['readiness']
                ready = readiness['access_ready'] and readiness['protection_ready']
                result = {'status': ('enabled' if readiness['enabled'] else 'ready') if ready else 'blocked',
                          'requirements': observed}
            else:
                observed = self.operation(action, self.target, self.requirements(self.target)['revision'])
                result = {'status': 'enabled' if observed['readiness']['enabled'] else 'disabled', 'requirements': observed}
            return self.result(result)
        except Pending:
            return self.result({'status': 'operation_pending', 'next_actions': [{'kind': 'resume'}]})

    def result(self, result):
        if result.get('status') in ('blocked', 'verification_pending') and 'next_actions' not in result:
            result = {**result, 'next_actions': [{'kind': 'open_url',
                'url': self.client.base + '/settings/repositories/' + str(self.target['id']),
                'reason': 'Inspect App access and review rules, repair the reported requirement, then resume setup.'}]}
        return {**result, 'workflow_id': self.state['id'],
                'operation_id': (self.state.get('operation') or {}).get('id') or self.state.get('last_operation'),
                'repository_url': self.client.base + '/settings/repositories/' + str(self.target['id'])}
