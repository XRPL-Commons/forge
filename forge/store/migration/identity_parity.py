"""Check identity rows without printing secrets, credential hashes or subjects."""
from ..identity import session_in, valid_connection_in
from ..db import _private
from .identity import AUTH, AUDIT, OPERATIONS, CREDENTIAL
from .snapshot import decode, digest, read_regular
from .supporting_parity import require


def check(importer):
    counts = dict(operator_sessions=0,operator_flows=0,client_connections=0,consumed_refresh_tokens=0,
                  client_pairings=0,operator_audit=0,client_operations=0)
    if AUTH not in importer.source.entries:
        return counts
    source = importer.source.json(AUTH)
    if not source:
        return counts
    now = importer.source.manifest['captured_at']
    with importer.store.read_snapshot() as db:
        config = db.execute('SELECT * FROM operator_auth_configuration').fetchone()
        require(config is not None and config['origin']==source['config']['origin'] and config['client_id']==source['config']['client_id']
                and config['source_epoch']==source['epoch'] and config['instance_id']==source['instance_id'],'identity_config')
        allowed = [row[0] for row in db.execute('SELECT github_id FROM operator_identities WHERE enabled=1 ORDER BY github_id')]
        require(allowed==sorted(set(source['config']['operators'])),'identity_operators')
        _private(importer.store.root / CREDENTIAL)
        credential = read_regular(importer.store.root / CREDENTIAL)
        require(credential==source['config']['client_secret'].encode() and digest(credential)==config['credential_digest']
                and config['credential_ref']==CREDENTIAL,'identity_credential')
        for hashed,value in source['sessions'].items():
            row = db.execute('SELECT s.*,i.github_id FROM operator_sessions s JOIN operator_identities i ON i.id=s.operator_id WHERE token_hash=?',(hashed,)).fetchone()
            require(row is not None and dict(operator={'id':row['github_id'],'login':row['operator_login']},csrf=row['csrf'],expires=row['expires_at'])==value,'identity_session')
            valid = value['expires']>now and value['operator']['id'] in source['config']['operators']
            require(bool(session_in(db,hashed,now))==valid,'identity_session_validity')
            counts['operator_sessions']+=1
        for hashed,value in source['flows'].items():
            row = db.execute('SELECT * FROM operator_flows WHERE state_hash=?',(hashed,)).fetchone()
            require(row is not None and row['nonce_hash']==value['browser'] and row['verifier']==value['verifier']
                    and row['return_path']==value.get('return_to','/') and row['expires_at']==value['expires']
                    and row['consumed_at'] is None and row['source_epoch']==source['epoch'],'identity_flow')
            counts['operator_flows']+=1
        generation = importer.source.json('state/onboarding/setup.json',{}).get('generation')
        for identifier,value in source.get('clients',{}).items():
            row = db.execute('SELECT c.*,i.github_id FROM client_connections c JOIN operator_identities i ON i.id=c.operator_id WHERE c.id=?',(identifier,)).fetchone()
            require(row is not None,'identity_connection')
            fields = [('source_epoch','epoch'),('instance_id','instance'),('setup_generation','generation'),('origin','origin'),
                      ('client_name','name'),('created_at','created'),('expires_at','expires'),('access_hash','access_hash'),
                      ('access_expires','access_expires'),('refresh_hash','refresh_hash'),('idle_expires','idle_expires'),
                      ('last_used_at','last_used'),('revocation_reason','revoked'),('next_refresh_at','next_refresh')]
            require(all(row[new]==value.get(old) for new,old in fields)
                    and {'id':row['github_id'],'login':row['operator_login']}==value['operator']
                    and decode(row['scopes_json'])==value['capabilities'] and decode(row['scope_json'])==value['scope'], 'identity_connection')
            tokens = [r[0] for r in db.execute('SELECT token_hash FROM consumed_refresh_tokens WHERE connection_id=? ORDER BY sequence',(identifier,))]
            require(tokens==value['used_refresh'],'refresh_replay_fence')
            valid = bool(not value.get('revoked') and value['epoch']==source['epoch'] and value['instance']==source['instance_id']
                         and value['generation']==generation and value['operator']['id'] in source['config']['operators']
                         and value['expires']>now and value['idle_expires']>now)
            require(valid_connection_in(db,row,generation,now)==valid,'connection_authorization')
            counts['client_connections']+=1
            counts['consumed_refresh_tokens']+=len(tokens)
        for hashed,value in source.get('devices',{}).items():
            row = db.execute('SELECT * FROM client_pairings WHERE device_hash=?',(hashed,)).fetchone()
            fields = [('user_code','user_code'),('instance_id','instance'),('setup_generation','generation'),('source_epoch','epoch'),
                      ('origin','origin'),('client_name','name'),('expires_at','expires'),('poll_interval','interval'),
                      ('next_poll_at','next_poll'),('status','status'),('connection_id','grant_id')]
            require(row is not None and all(row[new]==value.get(old) for new,old in fields)
                    and decode(row['capabilities_json'])==value['capabilities'] and decode(row['scope_json'])==value['scope'],'identity_pairing')
            counts['client_pairings']+=1
        if AUDIT in importer.source.entries:
            expected = [decode(line) for line in importer.source.read(AUDIT).splitlines() if line.strip()]
            rows = db.execute('SELECT * FROM operator_audit ORDER BY sequence').fetchall()
            actual = [dict(time=row['at'],route=row['action'],**decode(row['details_json'])) for row in rows]
            require(actual==expected,'operator_audit')
            counts['operator_audit']=len(rows)
        for identifier,value in importer.source.json(OPERATIONS,{}).items():
            row = db.execute('SELECT * FROM client_operations WHERE id=?',(identifier,)).fetchone()
            require(row is not None and decode(row['payload_json'])==value['request'] and row['created_at']==value['created_at']
                    and row['status']==('completed' if value['status']=='complete' else value['status'])
                    and row['finished_at']==value.get('finished_at') and row['error_code']==value.get('error')
                    and row['expected_revision']==value.get('expected_revision')
                    and (decode(row['result_json']) if row['result_json'] is not None else None)==value.get('result'),'client_operation')
            counts['client_operations']+=1
    return counts
