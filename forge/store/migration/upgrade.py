"""Resumable CLI maintenance for an already selected SQLite installation."""
import json
from pathlib import Path
import re
import time

from ..db import Store, _private
from ..files import atomic_json
from .. import upgrade as schema
from .snapshot import MigrationError, digest, operation_lock, read_regular
from .source import migration_files


OUTPUT_TAIL = 4000


def output_tail(text):
    """The bounded, scrubbed end of the updater's output for receipts and errors."""
    from ...factory.runner import scrub
    text = scrub(text if isinstance(text, str) else '')
    text = re.sub(r'\x1b\[[0-9;?]*[A-Za-z]|[^\S\n]+|[\x00-\x08\x0b-\x1f\x7f]', ' ', text)
    lines = [line.rstrip() for line in text.strip().splitlines() if line.strip()]
    return '\n'.join(lines[-40:])[-OUTPUT_TAIL:]


def package_version(source):
    """Bind every executable file transferred by the standalone upgrade tool."""
    directory = Path(source)
    paths = migration_files(directory)
    return '2:' + digest(b''.join(path.relative_to(directory).as_posix().encode() + read_regular(path) for path in paths))


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

    def run(self, *, revision, apply=False, source_pack=None):
        if not apply:
            return {'status': 'planned', **self.status()}
        if not isinstance(revision, str) or not re.fullmatch(r'[a-f0-9]{40}', revision):
            raise MigrationError('upgrade_revision_required')
        source_options = {'source_pack': source_pack} if source_pack is not None else {}
        with operation_lock(self.root, '.migration-host.lock'):
            tool_version = package_version(Path(__file__).resolve().parents[3])
            store = self.store()
            value = self.read() if self.path.exists() else None
            previous = value
            source_qualified = False
            advanced = None
            if value and value['source_revision'] != revision:
                if value['stage'] == 'complete':
                    # Completion releases source, service and drain ownership.
                    # A later application release can be the next upgrade's base.
                    previous = None
                else:
                    observed = self.host.identity()
                    if observed not in {value['previous_revision'], value['source_revision']}:
                        # An application update may have advanced the checkout
                        # after the schema committed. Re-enter full maintenance
                        # only after proving both legs of the forward history.
                        if value['stage'] != 'reconciling':
                            raise MigrationError('upgrade_source_changed')
                        self.host.qualify_advanced_source(value['source_revision'], observed)
                        advanced = dict(source_revision=value['source_revision'],
                                        stage=value['stage'], observed_revision=observed)
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
                if advanced and plan['source_revision'] != advanced['observed_revision']:
                    raise MigrationError('upgrade_source_changed')
                self.host.prepare_source_update(plan['source_revision'], revision, tool_version, **source_options)
                source_qualified = True
                value = dict(protocol=1, root=str(self.root), source=str(self.host.source), authorized=True,
                             previous_revision=plan['source_revision'], source_revision=revision,
                             tool_version=tool_version, services=sorted(set(self.host.units()) | set(previous['services'] if previous else [])),
                             started_at=time.time())
                if advanced:
                    value['superseded'] = advanced
                from .drain import Drain
                Drain(self, store).start(value, previous)
            if value['stage'] == 'reconciling' and self.host.units() and self.blockers(store):
                # A failed updater can already have restarted the supervisor.
                # Agents stay switched off until reconciling succeeds, so it
                # normally starts nothing and the retry just finishes. Only a
                # worker that started since the stop (none existed then)
                # re-enters maintenance, so it finishes through normal cleanup.
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
                # A new bundled tool can finish teardown that the installed
                # release cannot persist. Requalify resumed source intent before
                # invoking any corrected code against owned physical resources.
                if not source_qualified:
                    self.host.prepare_source_update(value['previous_revision'], revision, tool_version, **source_options)
                from .probe_cleanup import cleanup_fenced_probe
                cleanup_fenced_probe(self.root, store)
                from .auxiliary_cleanup import cleanup_unstarted_auxiliary
                cleanup_unstarted_auxiliary(self.root, store)
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
                self.host.prepare_source_update(value['previous_revision'], revision, tool_version, **source_options)
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
                # The supported updater rebuilds the UI and converges units. It
                # is repeatable after a disconnect, including after DB commit.
                # Agents are restored only after it succeeds: a failed updater
                # leaves services "not yet restored", so a supervisor it already
                # restarted takes no new work and a retry needs no new drain.
                import subprocess
                result = self.host.run([str(self.host.source/'bin/forge'), 'update', '--root', str(self.root), '--no-pull'],
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=900)
                tail = output_tail(getattr(result, 'stdout', ''))
                value['updater'] = dict(returncode=result.returncode, output_tail=tail, finished_at=time.time())
                self.save(value, 'reconciling')
                if result.returncode:
                    raise MigrationError('upgrade_host_update_failed', detail=tail or None)
                from .drain import Drain
                Drain(self, store).restore(value)
                if value['services']:
                    self.host.command('systemctl', '--user', 'start', *value['services'])
                if not set(value['services']).issubset(set(self.host.units())):
                    raise MigrationError('service_restore_incomplete')
                self.save(value, 'complete')
            return {'status': 'upgraded', **self.status()}
