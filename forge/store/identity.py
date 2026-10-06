"""Identity queries preserve both numeric epochs and imported opaque fences."""
import json


def session_in(db, hashed, now):
    row = db.execute('SELECT s.*,i.github_id FROM operator_sessions s '
                     'JOIN operator_identities i ON i.id=s.operator_id '
                     'JOIN operator_auth_configuration c ON c.singleton=1 '
                     'WHERE s.token_hash=? AND s.expires_at>? AND s.revoked_at IS NULL AND i.enabled=1 '
                     'AND s.authorization_epoch=i.authorization_epoch AND s.authorization_epoch=c.authorization_epoch',
                     (hashed,now)).fetchone()
    return None if row is None else dict(operator={'id':row['github_id'],'login':row['operator_login']},csrf=row['csrf'],expires=row['expires_at'])


def valid_connection_in(db, row, generation, now):
    if row is None:
        return False
    config = db.execute('SELECT * FROM operator_auth_configuration WHERE singleton=1').fetchone()
    operator = db.execute('SELECT * FROM operator_identities WHERE id=?',(row['operator_id'],)).fetchone()
    return bool(config and operator and operator['enabled'] and row['revoked_at'] is None
                and row['revocation_reason'] is None and row['source_epoch']==config['source_epoch']
                and row['authorization_epoch']==config['authorization_epoch']==operator['authorization_epoch']
                and row['instance_id']==config['instance_id'] and row['setup_generation']==generation
                and row['expires_at']>now and row['idle_expires']>now)


class IdentityQueries:
    def __init__(self, store):
        self.store = store

    def session(self, hashed, now):
        with self.store.read_snapshot() as db:
            return session_in(db,hashed,now)

    def bearer(self, hashed, *, generation, now):
        if not hashed:
            return None
        with self.store.read_snapshot() as db:
            row = db.execute('SELECT * FROM client_connections WHERE access_hash=?',(hashed,)).fetchone()
            if not valid_connection_in(db,row,generation,now) or row['access_expires']<=now:
                return None
            operator = db.execute('SELECT github_id FROM operator_identities WHERE id=?',(row['operator_id'],)).fetchone()
            return dict(id=row['id'],name=row['client_name'],operator={'id':operator[0],'login':row['operator_login']},
                        capabilities=json.loads(row['scopes_json']),scope=json.loads(row['scope_json']),created=row['created_at'],
                        expires=row['expires_at'],idle_expires=row['idle_expires'],last_used=row['last_used_at'],
                        instance=row['instance_id'],generation=row['setup_generation'],origin=row['origin'],status='active')
