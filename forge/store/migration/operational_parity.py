"""Independent comparisons of every operational source entity and artifact."""
from .snapshot import decode, digest
from .supporting_parity import require


def execution(db, identifier):
    rows=db.execute("SELECT sequence,digest,content FROM auxiliary_artifacts WHERE execution_id=? AND kind='legacy_execution' ORDER BY sequence",(identifier,)).fetchall()
    require(bool(rows),'auxiliary_execution')
    for index,row in enumerate(rows):
        require(row['sequence']==index and row['digest']==digest(row['content']),'auxiliary_artifact')
    value=decode(b''.join(row['content'] for row in rows))
    row=db.execute('SELECT * FROM auxiliary_executions WHERE id=?',(identifier,)).fetchone()
    require(row is not None and row['cleanup']==1 and row['fenced']==1 and value.get('cleanup') and row['created_at']==value.get('created_at'),'auxiliary_execution')
    return value


def check(importer):
    counts=dict(access_roles=0,access_repositories=0,repository_setup=0,observation_requests=0,setup_instances=0,auxiliary_executions=0,deployment_profiles=0)
    with importer.store.read_snapshot() as db:
        for scope,prefix in [('live',''),('setup','setup/')]:
            name=prefix+'state/github-access/access.json'
            if name in importer.source.entries:
                source=importer.source.json(name)
                row=db.execute('SELECT details_json FROM runtime_controls WHERE name=?',(scope+':access',)).fetchone()
                actual=decode(row[0]) if row else {}
                actual['roles']={}
                for row in db.execute('SELECT * FROM access_role_observations WHERE scope=?',(scope,)):
                    value=decode(row['details_json'])
                    value.update(status=row['status'],observed_at=row['observed_at'])
                    repos=db.execute('SELECT * FROM access_repository_observations WHERE scope=? AND role=? ORDER BY position',(scope,row['role'])).fetchall()
                    if repos or 'repositories' in source['roles'][row['role']]:
                        value['repositories']=[dict(id=r['repository_id'],name=r['full_name'],archived=bool(r['archived']),disabled=bool(r['disabled'])) for r in repos]
                    actual['roles'][row['role']]=value
                    counts['access_roles']+=1
                    counts['access_repositories']+=len(repos)
                require(actual==source,'access_observation')
            name=prefix+'state/github-access/sync.json'
            if name in importer.source.entries:
                row=db.execute('SELECT details_json FROM runtime_controls WHERE name=?',(scope+':access_sync',)).fetchone()
                require(row is not None and decode(row[0])==importer.source.json(name),'access_sync')
        for key,value in importer.source.json('state/github-access/repositories.json',{}).get('repositories',{}).items():
            row=db.execute('SELECT s.*,r.full_name FROM repository_setup_observations s JOIN repositories r ON r.id=s.repository_id WHERE repository_id=?',(int(key),)).fetchone()
            require(row is not None,'repository_setup')
            actual=dict(name=row['full_name'],status=row['status'],revision=row['source_revision'],started_at=row['started_at'],
                        previous_status=row['previous_status'],report=decode(row['report_json']) if row['report_json'] else None,
                        last_known=decode(row['last_known_json']) if row['last_known_json'] else None)
            require({k:v for k,v in actual.items() if k in decode(row['source_fields_json'])}==value,'repository_setup')
            counts['repository_setup']+=1
        for key,value in importer.source.json('state/factory/check-requests.json',{}).items():
            row=db.execute('SELECT requested_at FROM observation_requests WHERE issue_id=?',(key,)).fetchone()
            require(row is not None and row[0]==value,'observation_request')
            counts['observation_requests']+=1
        source=importer.source.json('state/onboarding/setup.json',{})
        if source and all(k in source for k in ('generation','phase')) and (source['phase']=='setup' or all(k in source for k in ('owner','account_type'))):
            row=db.execute("SELECT * FROM setup_instances WHERE scope='live'").fetchone()
            require(row is not None,'setup_instance')
            actual={k:row[k] for k in ('generation','phase','owner','account_type')}
            actual.update(decode(row['settings_json']))
            jobs=db.execute("SELECT id FROM auxiliary_executions WHERE domain='setup' AND owner_id='live' ORDER BY position").fetchall()
            actual['probe_jobs']=[execution(db,j[0]) for j in jobs]
            expected={**source, 'owner':source.get('owner') or '', 'account_type':source.get('account_type') or ''}
            require(actual==expected,'setup_instance')
            counts['setup_instances']+=1
            counts['auxiliary_executions']+=len(jobs)
        for key,source in importer.source.json('state/deployments.json',{}).get('repositories',{}).items():
            row=db.execute('SELECT p.*,r.full_name FROM deployment_profiles p JOIN repositories r ON r.id=p.repository_id WHERE repository_id=?',(int(key),)).fetchone()
            require(row is not None,'deployment_profile')
            actual=dict(name=row['full_name'],status=row['status'],workflows_digest=row['workflows_digest'],head=row['head_oid'],
                        job=execution(db,row['execution_id']) if row['execution_id'] else None,
                        summary=row['summary'],reason=row['reason'],error=row['error'],observed_at=row['observed_at'],checked_at=row['checked_at'],
                        reread_requested=bool(row['reread_requested']),exposure_issue=row['exposure_issue'])
            for key in ('proposal','observation','observed'):
                actual[key]=decode(row[key+'_json']) if row[key+'_json'] is not None else None
            require({k:v for k,v in actual.items() if k in decode(row['source_fields_json'])}==source,'deployment_profile')
            counts['deployment_profiles']+=1
            counts['auxiliary_executions']+=int(row['execution_id'] is not None)
        name='state/discussion-qualification.json'
        if name in importer.source.entries:
            source=importer.source.json(name)
            row=db.execute("SELECT details_json FROM runtime_controls WHERE name='discussion_qualification'").fetchone()
            require(row is not None,'discussion_qualification')
            actual=decode(row[0])
            if source.get('job'):
                actual['job']=execution(db,source['job']['id'])
                counts['auxiliary_executions']+=1
            elif 'job' in source:actual['job']=source['job']
            require(actual==source,'discussion_qualification')
    return counts
