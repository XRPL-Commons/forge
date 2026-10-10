"""Repository access, setup probes and deployment state entity mappings."""
from .normalize import batches, insert
from .supporting import json_text, unknown_fields
from .snapshot import MigrationError, encode, digest


def execution(db, domain, owner, position, job):
    if not job.get('cleanup'):
        raise MigrationError('uncleaned_auxiliary_execution')
    insert(db,'auxiliary_executions',id=job['id'],domain=domain,owner_id=owner,position=position,
           phase='cleaned',fenced=1,cleanup=1,created_at=job.get('created_at'))
    # Keep execution inputs and results off setup/status query paths. They are
    # available only through an explicit bounded artifact request.
    raw = encode(job)
    for sequence,start in enumerate(range(0,len(raw),1048576)):
        part = raw[start:start+1048576]
        insert(db,'auxiliary_artifacts',execution_id=job['id'],kind='legacy_execution',sequence=sequence,digest=digest(part),content=part)


def normalize(importer):
    mapped = set()
    for scope,prefix in [('live',''),('setup','setup/')]:
        name=prefix+'state/github-access/access.json'
        if name in importer.source.entries:
            source=importer.source.json(name)
            def role(db, pair):
                key,value=pair
                insert(db,'access_role_observations',scope=scope,role=key,status=value['status'],observed_at=value['observed_at'],
                       details_json=json_text({k:v for k,v in value.items() if k not in ('status','observed_at','repositories')}))
                for position,repo in enumerate(value.get('repositories',[])):
                    insert(db,'access_repository_observations',scope=scope,role=key,repository_id=repo['id'],full_name=repo['name'],
                           archived=int(repo['archived']),disabled=int(repo['disabled']),position=position)
                    unknown_fields(db,importer,name,repo,('id','name','archived','disabled'))
            batches(importer,'normalized:'+name,sorted(source.get('roles',{}).items()),role)
            batches(importer,'normalized:'+name+':header',[None],lambda db,_:
                    insert(db,'runtime_controls',name=scope+':access',heartbeat_at=source.get('observed_at'),updated_at=source.get('observed_at') or 0,
                           details_json=json_text({k:v for k,v in source.items() if k!='roles'})))
            mapped.add(name)
        name=prefix+'state/github-access/sync.json'
        if name in importer.source.entries:
            source=importer.source.json(name)
            batches(importer,'normalized:'+name,[None],lambda db,_:
                    insert(db,'runtime_controls',name=scope+':access_sync',heartbeat_at=source.get('finished_at'),
                           updated_at=source.get('finished_at') or source.get('started_at') or 0,details_json=json_text(source)))
            mapped.add(name)
    name='state/github-access/repositories.json'
    def repository(db,pair):
        key,value=pair
        row=db.execute('SELECT full_name FROM repositories WHERE id=?',(int(key),)).fetchone()
        if not row or row[0]!=value['name']:
            raise MigrationError('repository_setup_identity_mismatch')
        insert(db,'repository_setup_observations',repository_id=int(key),status=value['status'],source_revision=value['revision'],
               started_at=value['started_at'],previous_status=value.get('previous_status'),report_json=json_text(value.get('report')),
               last_known_json=json_text(value.get('last_known')),source_fields_json=json_text(sorted(value)))
        unknown_fields(db,importer,name,value,('name','status','revision','started_at','previous_status','report','last_known'))
    if name in importer.source.entries:
        source=importer.source.json(name)
        batches(importer,'normalized:'+name,sorted(source.get('repositories',{}).items()),repository)
        mapped.add(name)
    name='state/factory/check-requests.json'
    if name in importer.source.entries:
        batches(importer,'normalized:'+name,sorted(importer.source.json(name).items()),lambda db,pair:
                insert(db,'observation_requests',issue_id=pair[0],requested_at=pair[1]))
        mapped.add(name)
    name='state/onboarding/setup.json'
    if name in importer.source.entries:
        source=importer.source.json(name)
        if source and all(field in source for field in ('generation','phase')) and (source['phase']=='setup' or all(k in source for k in ('owner','account_type'))):
            def setup(db,_):
                value={k:v for k,v in source.items() if k not in ('generation','phase','owner','account_type','probe_jobs')}
                insert(db,'setup_instances',scope='live',generation=source['generation'],phase=source['phase'],owner=source.get('owner') or '',
                       account_type=source.get('account_type') or '',settings_json=json_text(value))
            batches(importer,'normalized:setup_instance',[None],setup)
            batches(importer,'normalized:setup_probes',list(enumerate(source.get('probe_jobs',[]))),lambda db,pair:
                    execution(db,'setup','live',pair[0],pair[1]))
            mapped.add(name)
        elif not source:
            mapped.add(name)
    name='state/deployments.json'
    if name in importer.source.entries:
        source=importer.source.json(name)
        if source.get('version')!=1:
            raise MigrationError('deployment_version_unsupported')
        def deployment(db,pair):
            key,value=pair
            repo=db.execute('SELECT full_name FROM repositories WHERE id=?',(int(key),)).fetchone()
            if not repo or repo[0]!=value['name']:
                raise MigrationError('deployment_repository_identity_mismatch')
            job=value.get('job')
            if job:execution(db,'deployment',key,0,job)
            insert(db,'deployment_profiles',repository_id=int(key),status=value['status'],workflows_digest=value.get('workflows_digest'),
                   head_oid=value.get('head'),execution_id=job['id'] if job else None,proposal_json=json_text(value.get('proposal')),
                   summary=value.get('summary'),reason=value.get('reason'),error=value.get('error'),observed_at=value.get('observed_at'),
                   checked_at=value.get('checked_at',0),reread_requested=int(value.get('reread_requested',False)),
                   observation_json=json_text(value.get('observation')),observed_json=json_text(value.get('observed')),
                   exposure_issue=value.get('exposure_issue'),source_fields_json=json_text(sorted(value)))
            unknown_fields(db,importer,name,value,('name','status','workflows_digest','head','job','proposal','summary','reason','error',
                                                  'observed_at','checked_at','reread_requested','observation','observed','exposure_issue'))
        batches(importer,'normalized:deployment_profiles',sorted(source.get('repositories',{}).items()),deployment)
        mapped.add(name)
    # This one-time diagnostic was created by an operator qualification command,
    # not by an ongoing runtime writer. Keep its execution ownership separately.
    name='state/discussion-qualification.json'
    if name in importer.source.entries:
        source=importer.source.json(name)
        def qualification(db,_):
            if source.get('job'):execution(db,'qualification','discussion',0,source['job'])
            insert(db,'runtime_controls',name='discussion_qualification',updated_at=0,
                   details_json=json_text({k:v for k,v in source.items() if k!='job'}))
        batches(importer,'normalized:discussion_qualification',[None],qualification)
        mapped.add(name)
    return mapped
