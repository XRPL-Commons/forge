"""Resumable CLI maintenance for an already selected SQLite installation."""
import json
from pathlib import Path
import re
import time

from ...state import _atomic_json as atomic_json
from ..db import Store, _private
from .. import upgrade as schema
from .snapshot import MigrationError, operation_lock, read_regular
from .cutover import package_version


class Upgrade:
    def __init__(self, host):
        self.host, self.root = host, host.root
        self.path = self.root / '.schema-upgrade.json'

    def store(self):
        marker = self.root / 'state/storage'
        _private(marker)
        rows = read_regular(marker, limit=4096).decode().splitlines()
        if len(rows) != 3 or rows[0] != 'forge-sqlite-v1' or not Path(rows[1]).is_absolute():
            raise MigrationError('runtime_selection_invalid')
        store = Store(rows[1])
        store.instance_id = rows[2]
        with schema.connection(store) as db:
            schema.validate_prefix(store, db)
            metadata = dict(db.execute("SELECT key,value FROM store_metadata WHERE key IN ('runtime_root','runtime_active','import_only')"))
            if metadata.get('runtime_root') != str(self.root) or metadata.get('runtime_active') != 'true' or metadata.get('import_only') == 'true':
                raise MigrationError('runtime_selection_invalid')
        return store

    def blockers(self, store):
        with schema.connection(store) as db:
            schema.validate_prefix(store, db)
            # Pipeline, discussion and auxiliary workers retain ownership until
            # verified cleanup; an empty scheduler queue alone is insufficient.
            count = sum(db.execute('SELECT COUNT(*) FROM '+table+' WHERE cleanup=0').fetchone()[0]
                        for table in ('jobs', 'discussion_executions', 'auxiliary_executions'))
            leases = sum(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
                         for table in ('job_leases', 'discussion_leases', 'auxiliary_leases'))
        return ['uncleaned_jobs'] if count or leases else []

    def status(self):
        store = self.store()
        result = {**schema.inspect(store), 'blockers': self.blockers(store), 'source_revision': self.host.identity()}
        if self.path.exists():
            result['stage'] = self.read()['stage']
        return result

    def read(self):
        _private(self.path)
        value = json.loads(read_regular(self.path, limit=65536))
        if (value.get('protocol') != 1 or value.get('root') != str(self.root)
                or value.get('source') != str(self.host.source) or value.get('authorized') is not True
                or value.get('stage') not in {'draining', 'stopping', 'installing', 'migrating', 'reconciling', 'complete'}
                or not all(isinstance(value.get(key), str) and re.fullmatch(r'[a-f0-9]{40}', value[key]) for key in ('previous_revision','source_revision'))
                or not isinstance(value.get('services'), list)
                or any(not isinstance(unit,str) or not re.fullmatch(r'forge[-a-z0-9]*\.(service|timer)',unit) for unit in value['services'])):
            raise MigrationError('invalid_schema_upgrade_journal')
        drain = value.get('drain')
        if drain is not None:
            if (not isinstance(drain, dict) or type(drain.get('original_enabled')) is not bool
                    or 'disable' not in drain or type(drain.get('restored', False)) is not bool):
                raise MigrationError('invalid_schema_upgrade_journal')
            for field in ('disable', 'restore'):
                change = drain.get(field)
                if change is not None and (not isinstance(change, dict)
                        or any(type(change.get(key)) is not int or change[key] < low
                               for key, low in (('before_revision', 0), ('after_revision', 1)))
                        or any(not isinstance(change.get(key), str) or not re.fullmatch(r'[a-f0-9]{64}', change[key])
                               for key in ('before_digest', 'after_digest'))
                        or change.get('enabled') is not (field == 'restore')):
                    raise MigrationError('invalid_schema_upgrade_journal')
        return value

    def save(self, value, stage):
        value.update(stage=stage, updated_at=time.time())
        atomic_json(self.path, value)

    def run(self, *, revision, apply=False):
        if not apply:
            return {'status': 'planned', **self.status()}
        if not isinstance(revision, str) or not re.fullmatch(r'[a-f0-9]{40}', revision):
            raise MigrationError('upgrade_revision_required')
        with operation_lock(self.root, '.migration-host.lock'):
            tool_version = package_version(Path(__file__).resolve().parents[3])
            store = self.store()
            value = self.read() if self.path.exists() else None
            previous = value
            if value and value['source_revision'] != revision:
                if value['stage'] == 'complete':
                    # Completion releases source, service and drain ownership.
                    # A later application release can be the next upgrade's base.
                    previous = None
                elif self.host.identity() not in {value['previous_revision'], value['source_revision']}:
                    # Forward repair still belongs to the unfinished operation.
                    raise MigrationError('upgrade_source_changed')
                value = None
            if value:
                if value['source_revision'] != revision or value['tool_version'] != tool_version:
                    raise MigrationError('upgrade_intent_changed')
                if value['stage'] == 'complete':
                    if self.host.identity() != revision:
                        raise MigrationError('upgrade_source_changed')
                    return {'status': 'upgraded', **self.status()}
            else:
                plan = self.status()
                self.host.prepare_source_update(plan['source_revision'], revision, tool_version)
                value = dict(protocol=1, root=str(self.root), source=str(self.host.source), authorized=True,
                             previous_revision=plan['source_revision'], source_revision=revision,
                             tool_version=tool_version, services=sorted(set(self.host.units()) | set(previous['services'] if previous else [])),
                             started_at=time.time())
                from .drain import Drain
                Drain(self, store).start(value, previous)
            if value['stage'] == 'reconciling' and self.host.units():
                # A failed updater can already have restarted the supervisor.
                # Re-enter maintenance before retrying any host changes so its
                # newly owned workers finish through the normal cleanup path.
                if self.host.identity() != revision or package_version(self.host.source) != tool_version:
                    raise MigrationError('upgrade_source_changed')
                previous = value
                value = {key:item for key,item in value.items() if key != 'drain'}
                value['services'] = sorted(set(value['services']) | set(self.host.units()))
                from .drain import Drain
                Drain(self, store).start(value, previous)
            if value['stage'] == 'draining':
                from .drain import Drain
                Drain(self, store).start(value)
                if self.blockers(store):
                    return {'status': 'draining', **self.status(), 'next_command': 'forge migrate upgrade'}
                self.save(value, 'stopping')
            if value['stage'] == 'stopping':
                if value['services']:
                    self.host.command('systemctl', '--user', 'stop', *value['services'])
                self.host.quiet()
                # Close the race between the first check and stopping admission.
                if self.blockers(store):
                    if value['services']:
                        self.host.command('systemctl', '--user', 'start', *value['services'])
                    self.save(value, 'draining')
                    return {'status': 'draining', **self.status(), 'next_command': 'forge migrate upgrade'}
                self.save(value, 'installing')
            if value['stage'] == 'installing':
                self.host.quiet()
                self.host.prepare_source_update(value['previous_revision'], revision, tool_version)
                self.host.install_source_update(value['previous_revision'], revision)
                self.save(value, 'migrating')
            if value['stage'] == 'migrating':
                self.host.quiet()
                if self.host.identity() != revision or package_version(self.host.source) != tool_version:
                    raise MigrationError('upgrade_source_changed')
                schema.apply(store)
                self.save(value, 'reconciling')
            if value['stage'] == 'reconciling':
                if self.host.identity() != revision or package_version(self.host.source) != tool_version:
                    raise MigrationError('upgrade_source_changed')
                from .drain import Drain
                Drain(self, store).restore(value)
                # The supported updater rebuilds the UI and converges units. It
                # is repeatable after a disconnect, including after DB commit.
                result = self.host.run([str(self.host.source/'bin/forge'), 'update', '--root', str(self.root), '--no-pull'],
                                       capture_output=True, text=True, timeout=900)
                if result.returncode:
                    raise MigrationError('upgrade_host_update_failed')
                if value['services']:
                    self.host.command('systemctl', '--user', 'start', *value['services'])
                if not set(value['services']).issubset(set(self.host.units())):
                    raise MigrationError('service_restore_incomplete')
                self.save(value, 'complete')
            return {'status': 'upgraded', **self.status()}
