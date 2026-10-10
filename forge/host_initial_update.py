"""Install current assets during a frozen legacy cutover; commit after selection.

The maintenance journal owns authorization and service start. This updater owns
its prepared managed-file receipt and the atomic configuration commit.
"""
from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import time

from . import host_update
from .state import _atomic_json
from .store.configuration import Configuration
from .store.db import _private
from .store.migration.host import Host
from .store.migration.snapshot import MigrationError, decode, digest, encode, read_regular
from .store.runtime import selected

PREPARED = '.migration-host-prepared.json'
COMMITTED = 'initial_host_update'


def context(root, operation_id, source_revision, *, prepare):
    root = Path(root).absolute()
    journal = decode(read_regular(root / '.migration-cutover.json', limit=1048576))
    initial = journal.get('initial_source')
    if (journal.get('protocol') != 1 or journal.get('authorized') is not True
            or journal.get('id') != operation_id or journal.get('root') != str(root)
            or journal.get('source') != str(host_update.SOURCE)
            or journal.get('source_revision') != source_revision
            or not isinstance(initial, dict) or initial.get('to_revision') != source_revision
            or journal.get('stage') not in ({'source_preparing'} if prepare else {'validated', 'selected', 'complete'})
            or host_update.source_revision() != source_revision):
        raise MigrationError('initial_host_update_intent_changed')
    host = Host(root, host_update.SOURCE)
    if prepare:
        if selected(root) is not None:
            raise MigrationError('initial_host_update_already_selected')
        host.quiet()
    return root, journal


class PreparedRecord(host_update.Record):
    """Persist installation progress outside the immutable source snapshot."""
    def __init__(self, root, operation_id, source_revision):
        self.root = root
        self.path = root / PREPARED
        original = host_update.Record(root).data
        if self.path.exists():
            _private(self.path)
            self.state = decode(read_regular(self.path, limit=262144))
            if (self.state.get('version') != 1 or self.state.get('operation_id') != operation_id
                    or self.state.get('source_revision') != source_revision
                    or self.state.get('original') != original):
                raise MigrationError('initial_host_update_receipt_changed')
        else:
            self.state = dict(version=1, operation_id=operation_id, source_revision=source_revision,
                              original=original, record=deepcopy(original), ready=False)
        self.data = self.state['record']

    def save(self, **values):
        self.data.update(values)
        _atomic_json(self.path, self.state)

    def finish(self, **values):
        original = self.state['original']
        self.save(**values, updated_at=time.time(),
                  previous={key: original.get(key) for key in ('source', 'image', 'ui_release')})
        units = Path.home() / '.config/systemd/user'
        paths = [host_update.APPARMOR_TARGET, self.root / 'runner-seccomp.json',
                 *[units / (name + '.service') for name in host_update.SERVICES],
                 *[self.root / 'deploy/runner' / name for name in ('compose.yaml', 'egress.py')]]
        self.state['assets'] = {str(path): digest(path.read_bytes()) for path in paths}
        self.state['ready'] = True
        self.save()
        return receipt(self.state)


def receipt(state):
    return dict(version=1, operation_id=state['operation_id'], source_revision=state['source_revision'],
                prepared_digest=digest(encode(state)),
                **{key: state['record'][key] for key in ('image', 'full_image', 'ui_release', 'bind')})


def verify_assets(root, state):
    record = state['record']
    if ((root / 'ui/current').resolve() != (root / 'ui/releases' / record['ui_release']).resolve()
            or not (root / 'ui/current/server/index.mjs').is_file()):
        raise MigrationError('initial_host_update_assets_changed')
    for filename, expected in state['assets'].items():
        path = Path(filename)
        if not path.is_file() or digest(path.read_bytes()) != expected:
            raise MigrationError('initial_host_update_assets_changed')
    for image in (record['image'], record['full_image']):
        if image is None:
            continue
        result = host_update.command(['docker', 'image', 'inspect', image, '--format', '{{.Id}}'],
                                     capture_output=True, text=True)
        if result.stdout.strip() != image:
            raise MigrationError('initial_host_update_assets_changed')


def prepare_initial(root, *, operation_id, source_revision):
    root, _ = context(root, operation_id, source_revision, prepare=True)
    record = PreparedRecord(root, operation_id, source_revision)
    if record.state['ready']:
        verify_assets(root, record.state)
        return receipt(record.state)
    # Shared reconciliation installs and qualifies assets/units, but this record
    # replaces all host.json writes and the activation branch is deferred.
    result = host_update._reconcile(root, assume_yes=True, prepared_record=record)
    verify_assets(root, record.state)
    return result


def commit_initial(root, supplied, *, operation_id, source_revision=None):
    source_revision = source_revision if source_revision is not None else supplied['source_revision']
    root, journal = context(root, operation_id, source_revision, prepare=False)
    store = selected(root)
    if store is None:
        raise MigrationError('initial_host_update_not_selected')
    path = root / PREPARED
    _private(path)
    state = decode(read_regular(path, limit=262144))
    if (not state.get('ready') or state.get('operation_id') != operation_id
            or state.get('source_revision') != source_revision or supplied != receipt(state)
            or journal['initial_source'].get('prepared') != supplied):
        raise MigrationError('initial_host_update_receipt_changed')
    committed = encode(dict(operation_id=operation_id, prepared_digest=supplied['prepared_digest'])).decode()
    with store.read_snapshot() as db:
        done = db.execute('SELECT value FROM store_metadata WHERE key=?', (COMMITTED,)).fetchone()
    if done:
        if done['value'] != committed:
            raise MigrationError('initial_host_update_commit_changed')
        return supplied
    Host(root, host_update.SOURCE).quiet()
    verify_assets(root, state)
    record = state['record']
    config = Configuration(store)
    with store.write() as db:
        done = db.execute('SELECT value FROM store_metadata WHERE key=?', (COMMITTED,)).fetchone()
        if done:
            if done['value'] != committed:
                raise MigrationError('initial_host_update_commit_changed')
            return supplied
        previous = db.execute("SELECT settings_json FROM configuration WHERE domain='host.json' AND subject='instance' AND active=1").fetchone()
        original = json.loads(previous[0]) if previous else {}
        original.setdefault('managed', {})
        if original != state['original']:
            raise MigrationError('initial_host_update_configuration_changed')
        for name in (*host_update.RUNTIME_DOMAINS, host_update.RECORD):
            current = db.execute('SELECT revision,settings_json,credential_ref FROM configuration '
                                 "WHERE domain=? AND subject='instance' AND active=1", (name,)).fetchone()
            if name == host_update.RECORD:
                value = record
            elif current:
                value = json.loads(current['settings_json'])
                value.update(image=record['image'], apparmor_profile=host_update.APPARMOR_PROFILE)
                if record['full_image']:
                    tiers = value.setdefault('environment_tiers', {})
                    tiers['full'] = {**{'memory': '4g', 'cpus': 2}, **tiers.get('full', {}), 'image': record['full_image']}
            else:
                continue
            config.replace_in(db, name, value, expected_revision=current['revision'] if current else 0,
                              credential_ref=current['credential_ref'] if current else None)
        db.execute('INSERT INTO store_metadata(key,value) VALUES(?,?)', (COMMITTED, committed))
    return supplied


@contextmanager
def diagnostic_output():
    """Keep FD 1 reserved for the receipt, including inherited child output."""
    sys.stdout.flush()
    saved = os.dup(1)
    try:
        os.dup2(2, 1)
        with redirect_stdout(sys.stderr):
            yield
    finally:
        sys.stderr.flush()
        os.dup2(saved, 1)
        os.close(saved)


def main():
    request = json.load(sys.stdin)
    if set(request) not in ({'action', 'root', 'operation_id', 'source_revision'},
                            {'action', 'root', 'operation_id', 'source_revision', 'receipt'}):
        raise MigrationError('invalid_initial_host_update_request')
    arguments = {key: request[key] for key in ('operation_id', 'source_revision')}
    with diagnostic_output():
        if request['action'] == 'prepare_initial' and 'receipt' not in request:
            result = prepare_initial(request['root'], **arguments)
        elif request['action'] == 'commit_initial' and 'receipt' in request:
            result = commit_initial(request['root'], request['receipt'], **arguments)
        else:
            raise MigrationError('invalid_initial_host_update_request')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
