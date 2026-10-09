"""Own only the temporary enabled=false change while schema maintenance drains.

Schema 19 supervisors already observe this setting and keep collecting existing
workers. The journal is written before the configuration transaction commits;
its exact revision/digest identifies a published change after a lost response.
"""
from contextlib import contextmanager
import hashlib
import json

from .. import upgrade as schema
from ..configuration import Configuration
from .snapshot import MigrationError

DOMAIN = 'runtime.json'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class Drain:
    def __init__(self, upgrade, store):
        self.upgrade, self.store = upgrade, store

    @contextmanager
    def transaction(self):
        with schema.connection(self.store, write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                schema.validate_prefix(self.store, db)
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def current(db):
        row = db.execute("SELECT revision,settings_json,credential_ref FROM configuration WHERE domain=? AND subject='instance' AND active=1", (DOMAIN,)).fetchone()
        return (row['revision'], json.loads(row['settings_json']), row['credential_ref']) if row else (0, {}, None)

    @staticmethod
    def published(db, change):
        if change is None:
            return False
        row = db.execute("SELECT settings_json FROM configuration WHERE domain=? AND subject='instance' AND revision=?", (DOMAIN, change['after_revision'])).fetchone()
        if row is None:
            return False
        return digest(json.loads(row[0])) == change['after_digest']

    @staticmethod
    def planned(db, revision, settings, enabled):
        target = {**settings, 'enabled': enabled}
        following = db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM configuration WHERE domain=? AND subject='instance'", (DOMAIN,)).fetchone()[0]
        return dict(before_revision=revision, before_digest=digest(settings),
                    after_revision=following, after_digest=digest(target), enabled=enabled)

    def publish(self, db, change):
        if self.published(db, change):
            return
        revision, settings, credential = self.current(db)
        if revision != change['before_revision'] or digest(settings) != change['before_digest']:
            raise MigrationError('drain_configuration_changed')
        following = Configuration(self.store).replace_in(db, DOMAIN, {**settings, 'enabled':change['enabled']},
            expected_revision=revision, credential_ref=credential)
        if following != change['after_revision']:
            raise MigrationError('drain_configuration_changed')

    def start(self, value, previous=None):
        with self.transaction() as db:
            if 'drain' not in value:
                inherited = (previous or {}).get('drain')
                # A forward repair owns an unfinished disable from the previous
                # release. A completed restoration starts a fresh drain instead.
                if inherited and not inherited.get('restored') and not self.published(db, inherited.get('restore')):
                    value['drain'] = inherited
                else:
                    revision, settings, _ = self.current(db)
                    enabled = settings.get('enabled', False)
                    if type(enabled) is not bool:
                        raise MigrationError('drain_runtime_invalid')
                    value['drain'] = dict(original_enabled=enabled, disable=self.planned(db, revision, settings, False) if enabled else None)
                self.upgrade.save(value, 'draining')
            change = value['drain']['disable']
            if change:
                self.publish(db, change)
            _, current, _ = self.current(db)
            if current.get('enabled', False) is not False:
                # The operator re-enabled the factory. Repeating --apply cannot
                # overwrite that newer instruction with the old drain intent.
                raise MigrationError('drain_configuration_changed')

    def restore(self, value):
        owned = value.get('drain')
        if not owned or owned.get('restored'):
            return
        if not owned['original_enabled']:
            # We did not disable this factory, so no setting belongs to us.
            owned['restored'] = True
            self.upgrade.save(value, 'reconciling')
            return
        with self.transaction() as db:
            change = owned.get('restore')
            if change and self.published(db, change):
                pass
            else:
                revision, settings, _ = self.current(db)
                if settings.get('enabled', False) != owned['original_enabled']:
                    # Preserve every other field from the operator's latest
                    # settings; only this temporary boolean belongs to us.
                    change = self.planned(db, revision, settings, owned['original_enabled'])
                    owned['restore'] = change
                    self.upgrade.save(value, 'reconciling')
                    self.publish(db, change)
        owned['restored'] = True
        self.upgrade.save(value, 'reconciling')
