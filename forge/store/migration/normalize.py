"""Conservative legacy-to-relational projection with explicit reconciliation findings."""
import time
import zlib
from ..transitions import manifest_hash, project_unit
from ...factory.ledger import Ledger
from .snapshot import MigrationError, encode, digest

LEDGER = 'state/factory/factory-ledger.json'
INVENTORY = 'state/issues/inventory.json'
PHASE = {'ready':'queued','built':'reviewing','reviewed':'auditing','audited':'merging','approval_needed':'merging'}


def insert(db, table, **values):
    # Table and column names are internal constants, never source-controlled SQL.
    db.execute(f"INSERT INTO {table}({','.join(values)}) VALUES({','.join('?' for _ in values)})", tuple(values.values()))


def finding(db, importer, subject, code, blocking=True):
    db.execute('INSERT INTO import_findings VALUES(?,?,?,?) ON CONFLICT DO NOTHING', (importer.run_id, subject, code, int(blocking)))


def gate(db, unit, code, at, details=None, candidate=None):
    subject = 'candidate:'+candidate if candidate else 'unit'
    insert(db, 'unit_gates', id=unit+':gate:'+code, unit_id=unit, candidate_id=candidate,
           subject_key=subject, code=code, created_at=at, details_json=encode(details or {}).decode())


def artifact(db, job, kind, value, at):
    raw = encode(value)
    for sequence, start in enumerate(range(0,len(raw),1024*1024)):
        part = raw[start:start+1024*1024]
        packed = zlib.compress(part)
        codec, content = ('zlib',packed) if len(packed)<len(part) else ('identity',part)
        insert(db,'job_artifacts',job_id=job,kind=kind,sequence=sequence,media_type='application/json',
               digest=digest(part),content=content,size=len(content),raw_size=len(part),codec=codec,created_at=at)


def batches(importer, name, rows, apply):
    with importer.store.read_snapshot() as db:
        cursor = importer._cursor(db, name)
    if cursor > len(rows):
        raise MigrationError('invalid_import_cursor')
    for start in range(cursor,len(rows),importer.batch):
        subset = rows[start:start+importer.batch]
        with importer.store.write() as db:
            if importer._cursor(db,name) != start:
                raise MigrationError('import_cursor_changed')
            for row in subset:
                apply(db,row)
            end = start+len(subset)
            db.execute('INSERT INTO import_progress VALUES(?,?,?, ?,0,?,?) ON CONFLICT(run_id,table_name) DO UPDATE SET cursor=excluded.cursor,imported_count=excluded.imported_count,complete=excluded.complete,updated_at=excluded.updated_at',
                       (importer.run_id,name,str(end),end,int(end==len(rows)),time.time()))
        if importer.after_batch:
            importer.after_batch(name,end)


def normalize(importer):
    ledger=importer.source.json(LEDGER)
    Ledger.validate(ledger)
    if ledger['slots'] or any(not j['cleanup'] for j in ledger['jobs'].values()):
        raise MigrationError('uncleaned_jobs')
    inventory=importer.source.json(INVENTORY,{'repositories':{}})
    runtime=importer.source.json('runtime.json',{})
    settings=importer.source.json('factory.json',{})
    configured={r['id']:r for r in settings.get('repositories',[])}
    repositories={int(k):v['name'] for k,v in inventory.get('repositories',{}).items()}
    access=importer.source.json('state/github-access/repositories.json',{}).get('repositories',{})
    operations=importer.source.json('state/client-operations/operations.json',{})
    for repo in [*settings.get('repositories',[]), *({'id':int(key),**value} for key,value in access.items()),
                 *(value['request']['repository'] for value in operations.values())]:
        if repo.get('name'):
            if repo['id'] in repositories and repositories[repo['id']]!=repo['name']:
                raise MigrationError('repository_identity_conflict')
            repositories[repo['id']]=repo['name']
    for issue in ledger['issues'].values():
        if issue['repo_id'] in repositories and repositories[issue['repo_id']]!=issue['repo']:
            raise MigrationError('repository_identity_conflict')
        repositories[issue['repo_id']]=issue['repo']
    batches(importer,'normalized:repositories',sorted(repositories.items()),lambda db,pair:
            insert(db,'repositories',id=pair[0],full_name=pair[1],enabled=int(configured.get(pair[0],{}).get('enabled',False))))
    observed={}
    for repo_id,repo in inventory.get('repositories',{}).items():
        for number,facts in repo.get('issues',{}).items():
            observed[f'{repo_id}:{number}']=(int(repo_id),facts,repo.get('at') or 0)
    for key,item in ledger['issues'].items():
        observation=item.get('github_observation') or {}
        fresh=observation.get('issue')
        old=observed.get(key)
        if fresh and (old is None or (fresh.get('updated_at') or '',observation.get('at') or 0) >= (old[1].get('updated_at') or '',old[2])):
            observed[key]=(item['repo_id'],fresh,observation.get('at') or 0)
        elif old is None:
            observed[key]=(item['repo_id'],dict(number=item['number'],title=item.get('title',''),body='',state='open'),item['created_at'])
    def add_issue(db,pair):
        key,(repo,facts,at)=pair
        item=ledger['issues'].get(key)
        pinned=item.get('requirements') if item else None
        body=facts.get('body') or ''
        requirement_hash=facts.get('content_hash') or manifest_hash(body)
        revision='legacy:revision:'+digest(encode([key,body,requirement_hash]))
        state=facts.get('state','open')
        reason=facts.get('state_reason')
        if reason not in (None,'completed','not_planned','duplicate','reopened'):
            finding(db,importer,key,'unknown_closure_reason');reason=None
        labels=[v['name'] if isinstance(v,dict) else v for v in facts.get('labels',[])]
        assignees=[v['login'] if isinstance(v,dict) else v for v in facts.get('assignees',[])]
        insert(db,'issues',id=key,repository_id=repo,github_number=facts['number'],github_state=state,
               state_reason=reason,current_revision_id=revision,remote_updated_at=facts.get('updated_at'),
               observed_at=at,labels_json=encode(labels).decode(),assignees_json=encode(assignees).decode(),
               assignee_types_json=encode(facts.get('assignee_types',{})).decode())
        insert(db,'issue_revisions',id=revision,issue_id=key,title=facts.get('title',''),body=body,
               content_hash=manifest_hash([facts.get('title',''),body]),requirements_hash=requirement_hash,
               requirements_json=encode({'legacy_body_hash':requirement_hash,'body_truncated':bool(facts.get('body_truncated'))}).decode(),observed_at=at)
        from ..presentation import record_revision_display
        record_revision_display(db,revision,body,{'body_truncated':bool(facts.get('body_truncated'))},source_content_hash=requirement_hash)
        from ..presentation import record_label_display
        record_label_display(db,key,labels,facts.get('label_events',[]))
        if facts.get('body_truncated'):
            finding(db,importer,key,'truncated_observation')
        if item:
            pinned_revision=revision
            if pinned and pinned != requirement_hash:
                pinned_revision='legacy:pinned:'+key
                insert(db,'issue_revisions',id=pinned_revision,issue_id=key,title=item.get('title',''),body='',
                       content_hash=manifest_hash([item.get('title',''),'']),requirements_hash=pinned,
                       requirements_json=encode({'legacy_body_hash':pinned,'body_unavailable':True}).decode(),observed_at=item['created_at'])
                record_revision_display(db,pinned_revision,'',{'body_unavailable':True},source_content_hash=pinned)
            add_unit(db,key,item,pinned_revision)
    merge_receipts={}
    for key,item in ledger['issues'].items():
        receipt=(item.get('evidence') or {}).get('merge') or {}
        if receipt.get('sha') and receipt.get('pr')==(item.get('candidate') or {}).get('pr'):
            merge_receipts[key]=receipt['sha']
    for event in ledger['events']:
        if event.get('kind')=='merged' and event.get('sha'):
            merge_receipts[event['issue']]=event['sha']
    def add_unit(db,key,item,revision):
        unit='legacy:unit:'+key
        original=item['state']; state=original
        if state in ('hold','failed','investigating'):
            saved=[h['state'] for field in ('operator_hold','environment_hold','prerequisite_hold')
                   if isinstance(h:=item.get(field),dict) and h.get('state') in PHASE]
            state=saved[0] if len(set(saved))==1 else None
        phase=PHASE.get(state,'queued')
        verification=item.get('verification_request')
        if original=='approval_needed' and verification:
            resume=verification.get('resume_state')
            if resume in ('built','reviewed'):
                phase=PHASE[resume]
            else:
                finding(db,importer,key,'verification_resume_stage_missing')
        merge_sha=merge_receipts.get(key)
        if original=='merged' and merge_sha:
            phase='merged'
        close_code=None
        if original=='cancelled':
            observed_issue=observed[key][1]
            reason=observed_issue.get('state_reason')
            if observed_issue.get('state')=='closed' and reason in ('completed','not_planned','duplicate'):
                phase='closed';close_code='github_'+reason
        candidate=item.get('candidate') or {}
        build=ledger['jobs'].get(candidate.get('build_attempt'),{})
        base=candidate.get('base_ref') or build.get('target_branch') or 'main'
        generation=2 if item['budget_start'] else 1
        insert(db,'units',id=unit,repository_id=item['repo_id'],base_branch=base,state=phase,close_code=close_code,
               merge_sha=merge_sha if phase=='merged' else None,
               current_candidate_id=('legacy:candidate:'+candidate['key']) if build and all(candidate.get(k) for k in ('pr','head','base','policy','requirements','key')) else None,
               version=max(1,item.get('decision_version',0)),
               budget_generation=generation,created_at=item['created_at'],updated_at=item.get('last_observed') or item['created_at'])
        terminal=phase in ('merged','closed')
        insert(db,'unit_issues',id='legacy:member:'+key,unit_id=unit,issue_id=key,repository_id=item['repo_id'],
               base_branch=base,requirements_revision_id=revision,state='done' if phase=='merged' else 'removed' if terminal else 'pending',
               joined_at=item['created_at'],left_at=(item.get('settled_observed') or item['created_at']) if terminal else None,
               exit_reason=phase if terminal else None)
        if not terminal:
            # A frozen import never authorizes work. The later cutover reconciler
            # must verify runtime ownership and every preserved legacy gate.
            gate(db,unit,'legacy_reconciliation',item['created_at'],{'source':LEDGER,'issue_key':key,'legacy_state':original})
            finding(db,importer,key,'live_unit_requires_reconciliation')
            for field,code in [('operator_hold','operator_backlog' if (item.get('operator_hold') or {}).get('kind')=='backlog' else 'operator_pause'),('prerequisite_hold','repository_setup'),
                               ('environment_hold','environment'),('label_hold','label_hold'),('operator_sync','operator_sync'),
                               ('ownership_wait','ownership_conflict'),('blocked_dependencies','dependency'),
                               ('decision_request','decision_required')]:
                if item.get(field):
                    gate(db,unit,code,item['created_at'],{'legacy_field':field,'value':item[field]})
            if Ledger.failure_count(item)>=3:
                gate(db,unit,'budget_exhausted',item['created_at'])
            if item.get('retry_after'):
                gate(db,unit,'retry_delay',item['created_at'],{'retry_at':item['retry_after']})
        if original=='merged' and not merge_sha:
            finding(db,importer,key,'merge_receipt_missing')
        if original=='cancelled' and not terminal:
            finding(db,importer,key,'ambiguous_legacy_cancelled')
        if candidate:
            add_candidate(db,unit,candidate,build,item['created_at'])
        if not terminal:
            current=db.execute('SELECT current_candidate_id FROM units WHERE id=?',(unit,)).fetchone()[0]
            verification=item.get('verification_request')
            if verification:
                if current and verification.get('candidate')==candidate.get('key'):
                    code='verification_ci' if verification.get('ci') else 'verification_human'
                    gate(db,unit,code,verification.get('created_at') or item['created_at'],verification,candidate=current)
                else:
                    finding(db,importer,key,'verification_candidate_mismatch')
            merge=item.get('merge_gate')
            if merge and merge.get('kind')!='ready':
                if current and merge.get('candidate')==candidate.get('key'):
                    code='approval_required' if merge.get('kind')=='approval' else 'merge_checks'
                    gate(db,unit,code,item['created_at'],merge,candidate=current)
                else:
                    finding(db,importer,key,'merge_gate_candidate_mismatch')
            if original=='investigating' or any(f.get('investigation')=='pending' for f in item['failures']):
                gate(db,unit,'recovery_pending',item['created_at'],{'source':LEDGER})
    def add_candidate(db,unit,candidate,build,at):
        required=('pr','head','base','policy','requirements','key')
        if not build or any(not candidate.get(k) for k in required):
            finding(db,importer,unit,'candidate_identity_incomplete');return
        candidate_id='legacy:candidate:'+candidate['key']
        assignment={k:v for k,v in build.items() if k not in ('capture','observation','ci_evidence')}
        insert(db,'candidates',id=candidate_id,unit_id=unit,pull_number=candidate['pr'],branch=candidate.get('branch') or build['branch'],
               head_oid=candidate['head'],base_oid=candidate['base'],policy_hash=candidate['policy'],requirements_hash=candidate['requirements'],
               membership_generation=1,input_manifest_hash=manifest_hash(assignment),accepted_at=candidate.get('accepted_at') or at)
    batches(importer,'normalized:issues',sorted(observed.items()),add_issue)
    completed_at={e['attempt']:e['at'] for e in ledger['events'] if e.get('kind')=='runner_released' and e.get('attempt')}
    def add_job(db,pair):
        identifier,job=pair;key=job['issue_key'];item=ledger['issues'][key];unit='legacy:unit:'+key
        failure_index=next((i for i,f in enumerate(item['failures']) if f['attempt']==identifier),None)
        generation=(1 if failure_index is not None and failure_index<item['budget_start'] else 2) if item['budget_start'] else 1
        assignment={k:v for k,v in job.items() if k not in ('capture','observation','ci_evidence')}
        manifest={'legacy_job_id':identifier,'source_manifest':importer.run_id,'assignment_digest':manifest_hash(assignment)}
        insert(db,'attempts',id=identifier,unit_id=unit,member_id='legacy:member:'+key,membership_generation=1,assignment_generation=1,
               budget_generation=generation,input_manifest_json=encode(manifest).decode(),created_at=job['created_at'])
        insert(db,'jobs',id=identifier,unit_id=unit,attempt_id=identifier,member_id='legacy:member:'+key,role=job['role'],
               phase='cleaned',outcome=job['outcome'],fenced=1,cleanup=1,created_at=job['created_at'])
        artifact(db,identifier,'launch_input',assignment,job['created_at'])
        capture=job.get('capture') or {}
        artifact(db,identifier,'result',{k:v for k,v in capture.items() if k!='logs'},job['created_at'])
        if 'logs' in capture:
            artifact(db,identifier,'logs',capture['logs'],completed_at.get(identifier,job['created_at']))
        if job.get('observation'):
            artifact(db,identifier,'live_tail',job['observation'],completed_at.get(identifier,job['created_at']))
        if job.get('ci_evidence'):
            artifact(db,identifier,'ci_transcript',job['ci_evidence'],completed_at.get(identifier,job['created_at']))
        if identifier not in completed_at:
            for row in db.execute("SELECT kind,sequence FROM job_artifacts WHERE job_id=? AND kind IN ('logs','live_tail','ci_transcript')",(identifier,)).fetchall():
                insert(db,'artifact_pins',job_id=identifier,kind=row['kind'],sequence=row['sequence'],purpose='legacy_completion_time_unknown',reference_id=identifier)
            finding(db,importer,identifier,'log_completion_time_unknown',blocking=False)
        if failure_index is not None:
            failure=item['failures'][failure_index]
            insert(db,'budget_entries',id=identifier+':charge',attempt_id=identifier,kind='charge',reason_code='legacy_charge',created_at=failure['at'])
            if not failure['charged']:
                insert(db,'budget_entries',id=identifier+':correction',attempt_id=identifier,kind='correction',corrects_id=identifier+':charge',reason_code='legacy_correction',created_at=failure['at'])
    batches(importer,'normalized:jobs',sorted(ledger['jobs'].items()),add_job)
    def add_evidence(db,pair):
        key,item=pair;unit='legacy:unit:'+key;candidate=item.get('candidate') or {}
        current=db.execute('SELECT current_candidate_id FROM units WHERE id=?',(unit,)).fetchone()[0]
        if not current:
            return
        merge=item.get('merge_gate') or {}
        if merge.get('candidate')==candidate.get('key') and merge.get('approvals'):
            from ..presentation import record_approval_display
            record_approval_display(db,current,merge['approvals'],item.get('last_observed') or item['created_at'],source='legacy')
        for role,value in item.get('evidence',{}).items():
            if role not in ('review','audit','merge'):
                finding(db,importer,key,'unknown_candidate_evidence');continue
            if role!='merge' and (value.get('candidate')!=candidate.get('key') or value.get('outcome')!='pass'):
                finding(db,importer,key,'candidate_evidence_mismatch');continue
            if role=='merge' and (value.get('pr')!=candidate.get('pr') or not value.get('sha')):
                finding(db,importer,key,'merge_evidence_mismatch');continue
            attempt=value.get('attempt')
            if attempt and attempt not in ledger['jobs']:
                finding(db,importer,key,'evidence_attempt_missing');continue
            at=ledger['jobs'][attempt]['created_at'] if attempt else candidate.get('accepted_at') or item['created_at']
            insert(db,'candidate_evidence',id=current+':'+role,unit_id=unit,candidate_id=current,attempt_id=attempt,kind=role,
                   verdict='pass',actor_id='legacy-controller',observed_at=at,details_json=encode(value).decode())
    batches(importer,'normalized:evidence',sorted(ledger['issues'].items()),add_evidence)
    def add_event(db,event):
        key=event.get('issue'); sequence=event['sequence']
        payload={'legacy_kind':event['kind'],'source':LEDGER,'source_record':['events',sequence-1],
                 'digest':digest(encode(event)),'legacy':event}
        insert(db,'events',sequence=sequence,transaction_id='legacy:event:'+str(sequence),unit_id='legacy:unit:'+key if key else None,
               issue_id=key,kind='legacy_event',at=event['at'],payload_json=encode(payload).decode())
    if [e['sequence'] for e in ledger['events']] != list(range(1,len(ledger['events'])+1)):
        raise MigrationError('legacy_event_sequence_invalid')
    batches(importer,'normalized:events',ledger['events'],add_event)
    from .supporting import normalize as supporting
    mapped=supporting(importer,ledger,inventory)
    from .configuration import normalize as configuration
    mapped.update(configuration(importer))
    from .identity import normalize as identity
    mapped.update(identity(importer))
    from .operational import normalize as operational
    mapped.update(operational(importer))
    from .context import normalize as context
    context(importer,ledger)
    with importer.store.write() as db:
        for key,item in ledger['issues'].items():
            view=project_unit(db,'legacy:unit:'+key)
            if view['charged_failures'] != Ledger.failure_count(item):
                raise MigrationError('budget_parity_mismatch')
            primary=view['current_blocker']
            db.execute('UPDATE units SET primary_gate_id=? WHERE id=?',(primary['id'] if primary else None,view['id']))
        for name,row in importer.source.entries.items():
            if name not in mapped | {LEDGER,INVENTORY} and row['domain'] not in ('diagnostic','independent_observer') and not name.endswith('.initialized'):
                finding(db,importer,name,'domain_mapping_pending')
