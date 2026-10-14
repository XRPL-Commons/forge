"""Identity queries preserve both numeric epochs and imported opaque fences."""


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
