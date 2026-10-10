"""Compare supporting rows and paged public messages with the frozen sources."""
from ..supporting_queries import SupportingQueries
from ...factory.capacity import memory_bytes
from .snapshot import MigrationError, decode, encode, digest


def require(condition, domain):
    if not condition:
        raise MigrationError(domain+'_parity_mismatch')


def check(importer):
    counts = dict(discussions=0,messages=0,discussion_pages=0,discussion_artifacts=0,
                  actions=0,notifications=0,inventory_repositories=0,inventory_pulls=0,inventory_scan_issues=0,inventory_scan_pages=0,provider_cooldowns=0,memory_profiles=0)
    reader = SupportingQueries(importer.store)
    ledger = importer.source.ledger()
    with importer.store.read_snapshot() as db:
        for account, value in ledger.get('provider_cooldowns',{}).items():
            row = db.execute('SELECT * FROM provider_cooldowns WHERE account_ref=?',(account,)).fetchone()
            if row is None and not ledger['jobs'].get(value.get('attempt'),{}).get('provider'):
                continue  # An explicit blocking finding, not silently qualified.
            require(row is not None and row['retry_at']==value['until'] and row['reason_code']==value['reason']
                    and row['source_job_id']==value['attempt'], 'provider_cooldown')
            counts['provider_cooldowns'] += 1
        for key, value in ledger.get('memory_profiles',{}).items():
            row = db.execute('SELECT * FROM memory_profiles WHERE id=?',('legacy:memory:'+key,)).fetchone()
            repo, role = key.split(':',1)
            require(row is not None and row['repository_id']==int(repo) and row['role']==role
                    and row['memory_bytes']==memory_bytes(value['memory']) and row['allocation']==value['memory']
                    and row['source_job_id']==value['attempt'] and row['observed_at']==value['at'], 'memory_profile')
            counts['memory_profiles'] += 1
        for key, source in importer.source.json('state/issues/inventory.json',{}).get('repositories',{}).items():
            row = db.execute("SELECT * FROM repository_observations WHERE id=? AND kind='inventory'", ('legacy:inventory:'+key,)).fetchone()
            require(row is not None and row['fetched_at']==(source.get('at') or 0)
                    and bool(row['complete'])==source.get('complete',False) and bool(row['refreshing'])==source.get('refreshing',False)
                    and row['controller_identity']==source.get('identity'), 'inventory')
            details = decode(row['details_json'])
            require(details.get('error')==source.get('error'), 'inventory')
            scan = source.get('scan')
            expected_header = {k:v for k,v in scan.items() if k != 'issues'} if isinstance(scan,dict) else scan
            expected_issues = (scan or {}).get('issues', {})
            after = None
            seen = 0
            while True:
                page = reader.inventory_scan(row['id'], after=after, limit=50)
                require(page['present']==('scan' in source) and page['header']==expected_header
                        and page['issues_present']==(isinstance(scan,dict) and 'issues' in scan), 'inventory_scan')
                expected_cursor = {k:scan[k] for k in ('state','page')} if scan and 'state' in scan else None
                require(page['cursor']==expected_cursor, 'inventory_scan')
                for entry in page['items']:
                    require(entry['key'] in expected_issues and entry['facts']==expected_issues[entry['key']], 'inventory_scan')
                    seen += 1
                counts['inventory_scan_pages'] += 1
                after = page['next_cursor']
                if after is None:
                    break
            require(seen==len(expected_issues), 'inventory_scan')
            counts['inventory_scan_issues'] += seen
            pulls = [dict(number=p['github_number'],title=p['title'],body=p['body'],author=p['author_login'],
                          branch=p['branch'],forge_owned=bool(p['forge_owned'])) for p in db.execute(
                          'SELECT * FROM inventory_pull_summaries WHERE repository_id=? ORDER BY github_number',(int(key),))]
            require(pulls==sorted(source.get('pulls',[]),key=lambda p:p['number']), 'inventory_pulls')
            collaborators = source.get('collaborators') or {}
            actual = [decode(p[0]) for p in db.execute("SELECT details_json FROM inventory_collaborators WHERE repository_id=? AND source='collaborator' ORDER BY login",(int(key),))]
            authors = [p[0] for p in db.execute("SELECT login FROM inventory_collaborators WHERE repository_id=? AND source='recent_author' ORDER BY login",(int(key),))]
            require(actual==sorted(collaborators.get('rows',[]),key=lambda p:p['login'])
                    and authors==sorted(collaborators.get('authors',[])) and details['collaborators_at']==collaborators.get('at'), 'inventory_collaborators')
            counts['inventory_repositories'] += 1
            counts['inventory_pulls'] += len(pulls)
        for key, source in importer.source.json('state/issues/discussions.json',{}).items():
            thread = db.execute('SELECT * FROM discussions WHERE issue_id=?',(key,)).fetchone()
            if thread is None:
                require(db.execute('SELECT 1 FROM issues WHERE id=?',(key,)).fetchone() is None,'discussion')
                continue
            for column,field in [('status','status'),('source_revision','revision'),('reason','reason'),('requested_at','requested_at'),('partial','partial')]:
                require(thread[column]==source.get(field),'discussion')
            require(bool(thread['cancel_requested'])==bool(source.get('cancel_requested')),'discussion')
            for column,field in [('origin','origin'),('presence_json','presence')]:
                require((decode(thread[column]) if thread[column] is not None else None)==source.get(field),'discussion')
            before, collected = None, []
            while True:
                page = reader.messages(key,before=before,limit=2)
                collected = page['messages']+collected
                counts['discussion_pages'] += 1
                before = page['before']
                if before is None:
                    break
            require(collected==source.get('messages',[]),'discussion_messages')
            counts['discussions'] += 1
            counts['messages'] += len(collected)
            job = source.get('job')
            if job:
                execution = db.execute('SELECT * FROM discussion_executions WHERE id=?',(job['id'],)).fetchone()
                require(execution is not None and execution['cleanup'] and execution['fenced']
                        and execution['source_created_at']==job.get('created_at')
                        and execution['completion_at']==job.get('completed'),'discussion_execution')
                chunks = db.execute("SELECT * FROM discussion_artifacts WHERE execution_id=? AND kind='launch_input' ORDER BY sequence",(job['id'],)).fetchall()
                for index,chunk in enumerate(chunks):
                    require(chunk['sequence']==index and chunk['digest']==digest(chunk['content']) and chunk['size']==len(chunk['content']),'discussion_artifact')
                require(decode(b''.join(c['content'] for c in chunks))==job,'discussion_artifact')
                counts['discussion_artifacts'] += len(chunks)
    for identifier, source in importer.source.json('state/issues/actions.json',{}).items():
        require(reader.action(identifier)==source,'action')
        counts['actions'] += 1
    for name in importer.source.entries:
        if name.startswith('state/notifications/outbox/') and name.endswith('.json'):
            source = importer.source.json(name)
            require(reader.notification(source['id'])==source,'notification')
            counts['notifications'] += 1
    return counts
