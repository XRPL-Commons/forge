"""Configuration revisions and per-repository environment scan mappings."""
from .normalize import batches, insert
from .supporting import json_text, unknown_fields
from .snapshot import MigrationError

CONFIGURATION = ('factory.json','runtime.json','host-runtime.json','host.json','setup/factory.json','setup/runtime.json')
CONTROLS = {
    'state/factory-v2.enabled':'factory_active',
    'state/factory-onboarding.enabled':'onboarding_active',
    'state/factory-paused.json':'factory_paused',
    'state/harness-login-enabled.json':'harness_login_enabled',
    'state/factory/supervisor.json':'supervisor',
    'state/setup-service.json':'setup_service',
}


def normalize(importer):
    mapped = set()
    names = [name for name in CONFIGURATION if name in importer.source.entries]
    batches(importer,'normalized:configuration',names,lambda db,name:
            insert(db,'configuration',domain=name,subject='instance',revision=1,active=1,
                   settings_json=json_text(importer.source.json(name)),created_at=importer.source.manifest['captured_at']))
    mapped.update(names)

    def control(db, name):
        value = importer.source.json(name)
        at = value.get('at') or value.get('activated_at') or 0
        insert(db,'runtime_controls',name=CONTROLS[name],enabled=1 if name.endswith('.enabled') or 'paused' in name or 'login-enabled' in name else None,
               source_generation=value.get('generation'),heartbeat_at=value.get('at'),updated_at=at,details_json=json_text(value))

    names = [name for name in CONTROLS if name in importer.source.entries]
    batches(importer,'normalized:controls',names,control)
    mapped.update(names)
    scans_name, proposals_name = 'state/environment-scans.json','state/environment-proposals.json'
    scans = importer.source.json(scans_name,{})
    proposals = importer.source.json(proposals_name,{})

    def environment(db, key):
        scan, proposal = scans.get(key),proposals.get(key)
        if scan is None:
            # An orphan proposal must not be attached to a fabricated successful scan.
            raise MigrationError('environment_scan_missing')
        identifier = 'legacy:scan:'+key
        insert(db,'environment_scans',id=identifier,repository_id=int(key),source_revision=proposal.get('head','') if proposal else '',
               status={'complete':'completed','running':'running','failed':'failed'}[scan['status']],
               created_at=scan['started_at'],source_id=scan.get('id'),error=scan.get('error'),source_fields_json=json_text(sorted(scan)))
        unknown_fields(db,importer,identifier,scan,('id','started_at','status','error'))
        if proposal:
            repo = db.execute('SELECT full_name FROM repositories WHERE id=?',(int(key),)).fetchone()
            if not repo or proposal['repo_id']!=int(key) or proposal['repo']!=repo[0]:
                raise MigrationError('environment_repository_identity_mismatch')
            insert(db,'environment_proposals',id='legacy:environment:'+key,scan_id=identifier,revision=1,status='proposed',
                   proposal_json=json_text(proposal),created_at=scan['started_at'])

    batches(importer,'normalized:environments',sorted(set(scans)|set(proposals)),environment)
    mapped.update(name for name in (scans_name,proposals_name) if name in importer.source.entries)
    requests_name = 'state/factory/operator-requests.json'

    def request(db, pair):
        identifier, value = pair
        insert(db,'operator_requests',id=identifier,action=value['action'],target=value['target'],reason=value['reason'],
               status=value['status'],result_json=json_text(value.get('result')))
        unknown_fields(db,importer,'request:'+identifier,value,('action','target','reason','status','result'))

    batches(importer,'normalized:operator_requests',sorted(importer.source.json(requests_name,{}).items()),request)
    if requests_name in importer.source.entries:
        mapped.add(requests_name)
    return mapped


def check(importer):
    from ..configuration import Configuration
    from .snapshot import decode
    counters = dict(configuration=0,controls=0,environments=0,operator_requests=0)
    for name in CONFIGURATION:
        if name in importer.source.entries:
            value = Configuration(importer.store).get(name)
            if not value or value['settings']!=importer.source.json(name):
                raise MigrationError('configuration_parity_mismatch')
            counters['configuration']+=1
    with importer.store.read_snapshot() as db:
        for name,identifier in CONTROLS.items():
            if name not in importer.source.entries:
                continue
            row = db.execute('SELECT * FROM runtime_controls WHERE name=?',(identifier,)).fetchone()
            value = importer.source.json(name)
            if not row or decode(row['details_json'])!=value or row['source_generation']!=value.get('generation'):
                raise MigrationError('control_parity_mismatch')
            counters['controls']+=1
        for key,source in importer.source.json('state/environment-scans.json',{}).items():
            row = db.execute('SELECT * FROM environment_scans WHERE id=?',('legacy:scan:'+key,)).fetchone()
            if not row:
                raise MigrationError('environment_parity_mismatch')
            value = dict(id=row['source_id'],error=row['error'],status='complete' if row['status']=='completed' else row['status'],started_at=row['created_at'])
            value = {k:v for k,v in value.items() if k in decode(row['source_fields_json'])}
            if value!=source:
                raise MigrationError('environment_parity_mismatch')
            counters['environments']+=1
        for key,source in importer.source.json('state/environment-proposals.json',{}).items():
            row = db.execute('SELECT proposal_json FROM environment_proposals WHERE id=?',('legacy:environment:'+key,)).fetchone()
            if not row or decode(row[0])!=source:
                raise MigrationError('environment_proposal_parity_mismatch')
        for key,source in importer.source.json('state/factory/operator-requests.json',{}).items():
            row = db.execute('SELECT * FROM operator_requests WHERE id=?',(key,)).fetchone()
            if not row:
                raise MigrationError('operator_request_parity_mismatch')
            value = {k:row[k] for k in ('action','target','reason','status')}
            if row['result_json'] is not None:
                value['result'] = decode(row['result_json'])
            if value!=source:
                raise MigrationError('operator_request_parity_mismatch')
            counters['operator_requests']+=1
    return counters
