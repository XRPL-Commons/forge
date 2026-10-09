"""Versioned configuration, kept separate from lifecycle and artifact queries."""
import json
import time


class ConfigurationConflict(ValueError):
    pass


class Configuration:
    def __init__(self, store, *, clock=time.time):
        self.store, self.clock = store, clock

    def get(self, domain, subject='instance'):
        with self.store.read_snapshot() as db:
            row = db.execute('SELECT revision,settings_json,credential_ref FROM configuration '
                             'WHERE domain=? AND subject=? AND active=1', (domain,subject)).fetchone()
            if row is None:
                return None
            return dict(revision=row['revision'],settings=json.loads(row['settings_json']),credential_ref=row['credential_ref'])

    def replace(self, domain, settings, *, expected_revision, subject='instance', credential_ref=None):
        with self.store.write() as db:
            return self.replace_in(db, domain, settings, expected_revision=expected_revision,
                                   subject=subject, credential_ref=credential_ref)

    def replace_in(self, db, domain, settings, *, expected_revision, subject='instance', credential_ref=None):
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError('expected_revision must be a nonnegative integer')
        payload = json.dumps(settings,sort_keys=True,separators=(',',':'),allow_nan=False)
        if len(payload.encode()) > 65536:
            raise ValueError('configuration exceeds its size limit')
        if db.execute("SELECT 1 FROM store_metadata WHERE key='import_only' AND value='true'").fetchone():
            raise ConfigurationConflict('staging imports cannot accept configuration changes')
        row = db.execute('SELECT revision,settings_json FROM configuration WHERE domain=? AND subject=? AND active=1', (domain,subject)).fetchone()
        current = row[0] if row else 0
        if current != expected_revision:
            raise ConfigurationConflict('configuration changed; reload before saving')
        following = db.execute('SELECT COALESCE(MAX(revision),0)+1 FROM configuration WHERE domain=? AND subject=?', (domain,subject)).fetchone()[0]
        db.execute('UPDATE configuration SET active=0 WHERE domain=? AND subject=? AND active=1',(domain,subject))
        db.execute('INSERT INTO configuration VALUES(?,?,?,?,?,?,?)',
                   (domain,subject,following,1,payload,credential_ref,self.clock()))
        from ..mutation_audit import config_change_in
        config_change_in(db, domain=domain, subject=subject,
                         before=json.loads(row['settings_json']) if row else None,
                         after=settings, revision=following, clock=self.clock)
        return following
