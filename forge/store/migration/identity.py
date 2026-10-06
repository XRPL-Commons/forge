"""Preserve identity, browser flows, connection revocations and refresh fences."""
import os
from ..db import _new_file, _private, _fsync_directory
from ...operator_auth import configuration, SESSION_SECONDS, FLOW_SECONDS
from .normalize import batches, insert
from .supporting import json_text, unknown_fields
from .snapshot import MigrationError, read_regular, digest, decode, operation_lock

AUTH = 'state/operator-auth/auth.json'
AUDIT = 'state/operator-auth/requests.jsonl'
OPERATIONS = 'state/client-operations/operations.json'
CREDENTIAL = 'credentials/operator-identity.secret'


def identities(source, audit):
    result = {str(identifier):{'id':identifier,'login':''} for identifier in source['config']['operators']}
    for value in [*source['sessions'].values(),*source.get('clients',{}).values(),*audit]:
        identity = value.get('operator')
        if identity:
            result[str(identity['id'])] = identity
    return result


def normalize(importer):
    if AUTH not in importer.source.entries:
        return set()
    source = importer.source.json(AUTH)
    if not source:
        return {AUTH}
    config = configuration(**source['config'])
    audit = [decode(line) for line in importer.source.read(AUDIT).splitlines() if line.strip()] if AUDIT in importer.source.entries else []
    credential = importer.store.root / CREDENTIAL
    if not credential.parent.exists():
        credential.parent.mkdir(mode=0o700)
        _fsync_directory(credential.parent.parent)
    _private(credential.parent,directory=True)
    raw = config['client_secret'].encode()
    with operation_lock(importer.store.root,'.identity-import.lock'):
        if credential.exists():
            _private(credential)
            if read_regular(credential)!=raw:
                raise MigrationError('identity_credential_conflict')
        else:
            # Publish only complete bytes. A process killed during the private
            # pending write can resume against the same sealed source secret.
            pending=credential.with_suffix('.pending')
            if os.path.lexists(pending):
                _private(pending)
                pending.unlink()
            _new_file(pending,raw)
            os.replace(pending,credential)
            _fsync_directory(credential.parent)

    def settings(db, _):
        insert(db,'operator_auth_configuration',singleton=1,origin=config['origin'],client_id=config['client_id'],
               credential_ref=CREDENTIAL,credential_digest=digest(raw),instance_id=source['instance_id'],
               source_epoch=source['epoch'],authorization_epoch=1)
        unknown_fields(db,importer,AUTH,source,
                       ('version','config','instance_id','epoch','sessions','flows','devices','clients','device_window','connection_lookups'))
        if source.get('version')!=1:
            raise MigrationError('identity_version_unsupported')

    batches(importer,'normalized:identity_configuration',[None],settings)
    batches(importer,'normalized:operator_identities', sorted(identities(source,audit).items()),lambda db,pair:
            insert(db,'operator_identities',id=pair[0],github_id=pair[1]['id'],login=pair[1]['login'],
                   enabled=int(pair[1]['id'] in config['operators']),authorization_epoch=1))

    def session(db, pair):
        hashed,value = pair
        insert(db,'operator_sessions',id=hashed,operator_id=str(value['operator']['id']),token_hash=hashed,
               authorization_epoch=1,created_at=value['expires']-SESSION_SECONDS,expires_at=value['expires'],csrf=value['csrf'],
               operator_login=value['operator']['login'])
        unknown_fields(db,importer,'session',value,('operator','csrf','expires'))

    batches(importer,'normalized:operator_sessions',sorted(source['sessions'].items()),session)

    def flow(db, pair):
        hashed,value = pair
        insert(db,'operator_flows',id=hashed,state_hash=hashed,nonce_hash=value['browser'],return_path=value.get('return_to','/'),
               created_at=value['expires']-FLOW_SECONDS,expires_at=value['expires'],verifier=value['verifier'],source_epoch=source['epoch'])
        unknown_fields(db,importer,'flow',value,('browser','verifier','expires','return_to'))

    batches(importer,'normalized:operator_flows',sorted(source['flows'].items()),flow)

    def connection(db, pair):
        identifier,value = pair
        if value['id']!=identifier:
            raise MigrationError('connection_identity_mismatch')
        insert(db,'client_connections',id=identifier,operator_id=str(value['operator']['id']),token_hash='connection:'+identifier,
               client_name=value['name'],authorization_epoch=1,scopes_json=json_text(value['capabilities']),
               created_at=value['created'],expires_at=value['expires'],
               revoked_at=importer.source.manifest['captured_at'] if value.get('revoked') else None,
               source_epoch=value['epoch'],instance_id=value['instance'],setup_generation=value['generation'],origin=value['origin'],
               scope_json=json_text(value['scope']),access_hash=value['access_hash'],access_expires=value['access_expires'],
               refresh_hash=value['refresh_hash'],idle_expires=value['idle_expires'],last_used_at=value.get('last_used'),
               revocation_reason=value.get('revoked'),next_refresh_at=value.get('next_refresh'),operator_login=value['operator']['login'])
        for sequence,hashed in enumerate(value['used_refresh']):
            insert(db,'consumed_refresh_tokens',token_hash=hashed,connection_id=identifier,sequence=sequence)
        unknown_fields(db,importer,'connection',value,('id','operator','name','capabilities','created','expires','revoked',
            'epoch','instance','generation','origin','scope','access_hash','access_expires','refresh_hash','idle_expires','last_used','used_refresh','next_refresh'))

    batches(importer,'normalized:client_connections',sorted(source.get('clients',{}).items()),connection)

    def device(db, pair):
        hashed,value = pair
        insert(db,'client_pairings',device_hash=hashed,user_code=value['user_code'],instance_id=value['instance'],
               setup_generation=value['generation'],source_epoch=value['epoch'],origin=value['origin'],client_name=value['name'],
               capabilities_json=json_text(value['capabilities']),scope_json=json_text(value['scope']),expires_at=value['expires'],
               poll_interval=value['interval'],next_poll_at=value['next_poll'],status=value['status'],connection_id=value.get('grant_id'))
        unknown_fields(db,importer,'pairing',value,('user_code','instance','generation','epoch','origin','name','capabilities','scope','expires','interval','next_poll','status','grant_id'))

    batches(importer,'normalized:client_pairings',sorted(source.get('devices',{}).items()),device)
    windows = [('device','instance',source['device_window'])] if 'device_window' in source else []
    windows += [('lookup',key,value) for key,value in sorted(source.get('connection_lookups',{}).items())]
    batches(importer,'normalized:auth_rate_limits',windows,lambda db,row:
            insert(db,'auth_rate_limits',kind=row[0],subject=row[1],window_start=row[2]['start'],request_count=row[2]['count']))

    def entry(db, pair):
        sequence,value = pair
        insert(db,'operator_audit',sequence=sequence,operator_id=str(value['operator']['id']),action=value['route'],
               outcome='admitted',at=value['time'],details_json=json_text({'event':value['event'],'operator':value['operator']}))
        unknown_fields(db,importer,AUDIT,value,('time','event','operator','route'))

    batches(importer,'normalized:operator_audit',list(enumerate(audit,1)),entry)

    def operation(db, pair):
        identifier,value = pair
        request = value['request']
        if request['id']!=identifier:
            raise MigrationError('client_operation_identity_mismatch')
        insert(db,'client_operations',id=identifier,request_id=identifier,connection_id=request['client'],repository_id=request['repository']['id'],
               kind=request['action'],status='completed' if value['status']=='complete' else value['status'],payload_json=json_text(request),
               result_json=json_text(value.get('result')),created_at=value['created_at'],updated_at=value.get('finished_at',value['created_at']),
               finished_at=value.get('finished_at'),error_code=value.get('error'),expected_revision=value.get('expected_revision'))
        unknown_fields(db,importer,OPERATIONS,value,('request','status','created_at','finished_at','error','result','expected_revision'))

    batches(importer,'normalized:client_operations',sorted(importer.source.json(OPERATIONS,{}).items()),operation)
    return {name for name in (AUTH,AUDIT,OPERATIONS) if name in importer.source.entries}
