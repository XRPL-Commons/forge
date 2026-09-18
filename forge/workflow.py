"""Transactional workflow authority. Callers supply verified external evidence.

No network/process calls, elapsed-time lease expiry, or guessed issue assignment.
GitHub facts enter through reconcile; execution authority enters through reserve
and exact-generation transitions. All authoritative changes include an event and
outbox row in the same SQLite transaction.
"""
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .state import PROVIDERS

STAGES = {'queued', 'build', 'review', 'audit', 'done', 'cancelled', 'unknown'}
DISPOSITIONS = {'pending', 'working', 'critical', 'stalled'}
ROLES = {'build', 'review', 'audit'}
LIVE = {'reserved', 'submitted', 'working', 'handoff', 'blocked', 'uncertain'}
GENERATION = re.compile(r'\$[0-9]+\|[0-9]+\|%[0-9]+\|[1-9][0-9]*')
SCHEMA = """
CREATE TABLE IF NOT EXISTS repositories(repo TEXT PRIMARY KEY, revision TEXT NOT NULL, observed_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS issues(repo TEXT NOT NULL, number INTEGER NOT NULL, stage TEXT NOT NULL,
 disposition TEXT NOT NULL, reason TEXT, revision TEXT NOT NULL, observed_at REAL NOT NULL,
 PRIMARY KEY(repo,number));
CREATE TABLE IF NOT EXISTS pr_links(repo TEXT NOT NULL, issue_number INTEGER NOT NULL,
 number INTEGER NOT NULL, head TEXT, base TEXT, PRIMARY KEY(repo,issue_number,number),
 FOREIGN KEY(repo,issue_number) REFERENCES issues(repo,number));
CREATE TABLE IF NOT EXISTS assignments(id TEXT PRIMARY KEY, repo TEXT NOT NULL, role TEXT NOT NULL,
 session TEXT NOT NULL, revision TEXT NOT NULL, issue_numbers TEXT NOT NULL, pr TEXT,
 state TEXT NOT NULL, provider TEXT, generation TEXT, attempt INTEGER NOT NULL DEFAULT 1,
 reason TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL, execution TEXT, submitted_at REAL);
CREATE UNIQUE INDEX IF NOT EXISTS active_role ON assignments(repo,role) WHERE state != 'retired';
CREATE UNIQUE INDEX IF NOT EXISTS active_session ON assignments(session) WHERE state != 'retired';
CREATE UNIQUE INDEX IF NOT EXISTS active_pr_stage ON assignments(repo,role,json_extract(pr,'$.number'))
 WHERE state != 'retired' AND pr IS NOT NULL;
CREATE TABLE IF NOT EXISTS slots(session TEXT PRIMARY KEY, repo TEXT NOT NULL, role TEXT NOT NULL,
 state TEXT NOT NULL, reason TEXT, assignment_id TEXT, provider TEXT, generation TEXT,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, bootstrap INTEGER NOT NULL DEFAULT 0, execution TEXT, FOREIGN KEY(assignment_id) REFERENCES assignments(id));
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, source_event_id TEXT NOT NULL UNIQUE,
 operation TEXT NOT NULL, request_hash TEXT NOT NULL, repo TEXT NOT NULL, assignment_id TEXT,
 at REAL NOT NULL, payload TEXT NOT NULL, result TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS workflow_events_no_update BEFORE UPDATE ON events
 BEGIN SELECT RAISE(ABORT,'append-only workflow history'); END;
CREATE TRIGGER IF NOT EXISTS workflow_events_no_delete BEFORE DELETE ON events
 BEGIN SELECT RAISE(ABORT,'append-only workflow history'); END;
CREATE TABLE IF NOT EXISTS refresh_receipts(source_event_id TEXT PRIMARY KEY, operation TEXT NOT NULL, request_hash TEXT NOT NULL, result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox(event_id INTEGER PRIMARY KEY, delivered_at REAL,
 FOREIGN KEY(event_id) REFERENCES events(id));
"""


class Refused(ValueError):
    """A requested transition lacks the required current evidence."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _repo(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',value):
        raise Refused('invalid_repo')
    return value.lower()


def _text(value, name, limit=256):
    if not isinstance(value,str) or not value or len(value)>limit or any(ord(c)<32 for c in value):
        raise Refused('invalid_' + name)
    return value


def _number(value):
    if type(value) is not int or value<=0:
        raise Refused('invalid_issue_or_pr_number')
    return value


def _time(value):
    if type(value) not in (int,float) or not math.isfinite(value) or value<0:
        raise Refused('invalid_timestamp')
    return float(value)


def _generation(value):
    if not isinstance(value,str) or not GENERATION.fullmatch(value):
        raise Refused('invalid_generation')
    return value


def _pr(value):
    if value is None:
        return None
    if not isinstance(value,dict):
        raise Refused('invalid_pr')
    result={'number':_number(value.get('number'))}
    for key in ('head','base'):
        item=value.get(key)
        if item is not None and (not isinstance(item,str) or not re.fullmatch('[0-9a-f]{40,64}',item)):
            raise Refused('invalid_pr_' + key)
        result[key]=item
    return result


def _path(state):
    return Path(state)/'workflow.sqlite3'


@contextmanager
def _database(state, *, write=False):
    path=_path(state)
    if path.is_symlink():
        raise Refused('workflow_database_symlink')
    marker=path.parent/'workflow.initialized'
    if marker.exists() and not path.exists():raise Refused('workflow_database_missing')
    if write:
        path.parent.mkdir(parents=True,exist_ok=True)
        fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        os.close(fd)
    elif not path.exists():
        raise FileNotFoundError(path)
    db=sqlite3.connect(str(path) if write else path.resolve().as_uri()+'?mode=ro',
                       uri=not write, timeout=1, isolation_level=None)
    db.row_factory=sqlite3.Row
    try:
        db.execute('PRAGMA foreign_keys=ON')
        if write:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            db.executescript(SCHEMA)
            if not marker.exists():
                try:
                    marker_fd=os.open(marker,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                except FileExistsError:pass
                else:
                    with os.fdopen(marker_fd,'w') as stream:
                        stream.write('{"schema":1}\n');stream.flush();os.fsync(stream.fileno())
                    directory_fd=os.open(path.parent,os.O_DIRECTORY)
                    try:os.fsync(directory_fd)
                    finally:os.close(directory_fd)
            db.execute('BEGIN IMMEDIATE')
        yield db
        if write:
            db.commit()
    except BaseException:
        if write:
            db.rollback()
        raise
    finally:
        db.close()


def _assignment(row):
    if row is None:
        raise Refused('assignment_unknown')
    value=dict(row)
    value['issue_numbers']=json.loads(value['issue_numbers'])
    value['pr']=json.loads(value['pr']) if value['pr'] else None
    value['execution']=json.loads(value['execution']) if value['execution'] else None
    return value


def _replay(db, event_id, operation, request):
    _text(event_id,'source_event_id')
    fingerprint=hashlib.sha256(_json(request).encode()).hexdigest()
    row=db.execute('SELECT operation,request_hash,result FROM events WHERE source_event_id=?',(event_id,)).fetchone()
    if row is None:
        row=db.execute('SELECT operation,request_hash,result FROM refresh_receipts WHERE source_event_id=?',(event_id,)).fetchone()
    if row:
        if row['operation']!=operation or row['request_hash']!=fingerprint:
            raise Refused('source_event_conflict')
        return fingerprint,json.loads(row['result'])
    return fingerprint,None


def _event(db, event_id, operation, fingerprint, repo, assignment_id, now, payload, result):
    cursor=db.execute('INSERT INTO events(source_event_id,operation,request_hash,repo,assignment_id,at,payload,result) VALUES(?,?,?,?,?,?,?,?)',
        (event_id,operation,fingerprint,repo,assignment_id,now,_json(payload),_json(result)))
    db.execute('INSERT INTO outbox(event_id) VALUES(?)',(cursor.lastrowid,))
    return result


def reconcile(state, repo, revision, observed_at, issues, *, source_event_id, now=None):
    """Import verified facts. Missing items are retained, never inferred closed."""
    repo=_repo(repo);revision=_text(revision,'revision');observed_at=_time(observed_at)
    now=_time(time.time() if now is None else now)
    if observed_at>now:
        raise Refused('future_observation')
    if not isinstance(issues,list) or len(issues)>10000:
        raise Refused('invalid_issue_inventory')
    normalized=[];seen=set()
    for item in issues:
        if not isinstance(item,dict) or not isinstance(item.get('prs',[]),list):raise Refused('invalid_issue_inventory')
        number=_number(item.get('number'));stage={'backlog':'queued','ready':'queued'}.get(item.get('stage'),item.get('stage'));disposition=item.get('disposition','pending')
        if number in seen or stage not in STAGES or disposition not in DISPOSITIONS-{'working'}:
            raise Refused('invalid_issue_state')
        seen.add(number)
        reason=item.get('reason')
        if reason is not None:_text(reason,'reason')
        prs=[_pr(value) for value in item.get('prs',[])]
        if any(pr is None for pr in prs) or len({pr['number'] for pr in prs})!=len(prs):
            raise Refused('invalid_pr_links')
        normalized.append(dict(number=number,stage=stage,disposition=disposition,reason=reason,prs=prs))
    request=dict(repo=repo,revision=revision,observed_at=observed_at,issues=normalized)
    with _database(state,write=True) as db:
        fingerprint,replay=_replay(db,source_event_id,'reconcile',request)
        if replay is not None:return replay
        prior=db.execute('SELECT * FROM repositories WHERE repo=?',(repo,)).fetchone()
        if prior and observed_at<prior['observed_at']:
            return _event(db,source_event_id,'reconcile',fingerprint,repo,None,now,request,{'status':'ignored_stale','revision':prior['revision']})
        if prior and observed_at==prior['observed_at'] and revision!=prior['revision']:
            raise Refused('observation_revision_conflict')
        before=[]
        changed=not prior or prior['revision']!=revision
        for item in normalized:
            row=db.execute('SELECT * FROM issues WHERE repo=? AND number=?',(repo,item['number'])).fetchone()
            before.append(dict(row) if row else None)
            disposition=item['disposition']
            active=db.execute("SELECT role,state,reason,issue_numbers,pr FROM assignments WHERE repo=? AND state!='retired'",(repo,)).fetchall()
            assigned=[a for a in active if item['number'] in json.loads(a['issue_numbers'])]
            if item['stage']=='queued' and any(a['role']=='build' for a in assigned):item['stage']='build'
            if disposition=='pending' and item['stage'] not in {'done','cancelled','unknown'}:
                matching=[a for a in assigned if a['role']==item['stage'] and
                          (not a['pr'] or json.loads(a['pr']) in item['prs'])]
                if any(a['state']=='working' for a in matching):disposition='working'
                blocked=next((a for a in matching if a['state'] in {'blocked','uncertain'}),None)
                if blocked:disposition='stalled';item['reason']=blocked['reason']
            old_prs=[dict(p) for p in db.execute('SELECT number,head,base FROM pr_links WHERE repo=? AND issue_number=? ORDER BY number',(repo,item['number']))]
            if (not row or any(row[k]!=v for k,v in {'stage':item['stage'],'disposition':disposition,'reason':item['reason'],'revision':revision}.items())
                    or old_prs!=sorted(item['prs'],key=lambda p:p['number'])):changed=True
            db.execute('INSERT INTO issues VALUES(?,?,?,?,?,?,?) ON CONFLICT(repo,number) DO UPDATE SET stage=excluded.stage,disposition=excluded.disposition,reason=excluded.reason,revision=excluded.revision,observed_at=excluded.observed_at',
                (repo,item['number'],item['stage'],disposition,item['reason'],revision,observed_at))
            db.execute('DELETE FROM pr_links WHERE repo=? AND issue_number=?',(repo,item['number']))
            for pr in item['prs']:
                db.execute('INSERT INTO pr_links VALUES(?,?,?,?,?)',(repo,item['number'],pr['number'],pr['head'],pr['base']))
        db.execute('INSERT INTO repositories VALUES(?,?,?) ON CONFLICT(repo) DO UPDATE SET revision=excluded.revision,observed_at=excluded.observed_at',(repo,revision,observed_at))
        if not changed:
            result={'status':'refreshed','revision':revision,'issues':len(normalized)}
            db.execute('INSERT INTO refresh_receipts VALUES(?,?,?,?)',(source_event_id,'reconcile',fingerprint,_json(result)))
            return result
        return _event(db,source_event_id,'reconcile',fingerprint,repo,None,now,dict(before=before,after=normalized,revision=revision),{'status':'applied','revision':revision,'issues':len(normalized)})


def reserve(state, repo, role, session, revision, issue_numbers, *, source_event_id, pr=None, provider=None, max_slots=None, max_per_role=None, now=None):
    """Reserve after adapter eligibility proof; revision CAS prevents stale admission."""
    repo=_repo(repo);_text(revision,'revision');_text(session,'session',120)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+',session) or role not in ROLES:
        raise Refused('invalid_role_session')
    if provider is not None and provider not in PROVIDERS:raise Refused('invalid_provider')
    if not isinstance(issue_numbers,list):raise Refused('invalid_issue_numbers')
    numbers=sorted({_number(n) for n in issue_numbers});pr=_pr(pr)
    if len(numbers)!=len(issue_numbers) or not numbers and pr is None:
        raise Refused('invalid_assignment_target')
    if role!='build' and (pr is None or not pr['head'] or not pr['base']):
        raise Refused('review_audit_requires_current_pr')
    for limit in (max_slots,max_per_role):
        if limit is not None and (type(limit) is not int or limit<1):raise Refused('invalid_capacity_limit')
    request=dict(repo=repo,role=role,session=session,revision=revision,issue_numbers=numbers,pr=pr,provider=provider,max_slots=max_slots,max_per_role=max_per_role)
    now=_time(time.time() if now is None else now)
    with _database(state,write=True) as db:
        fingerprint,replay=_replay(db,source_event_id,'reserve',request)
        if replay is not None:return replay
        snapshot=db.execute('SELECT * FROM repositories WHERE repo=?',(repo,)).fetchone()
        if not snapshot or snapshot['revision']!=revision:raise Refused('snapshot_revision_changed')
        if not 0<=now-snapshot['observed_at']<=300:raise Refused('snapshot_not_fresh')
        for number in numbers:
            issue=db.execute('SELECT * FROM issues WHERE repo=? AND number=?',(repo,number)).fetchone()
            if not issue or issue['revision']!=revision:raise Refused('issue_not_in_current_snapshot')
            if issue['stage'] not in ({'queued','build'} if role=='build' else {role}) or issue['disposition']!='pending':raise Refused('issue_not_dispatchable')
            if pr:
                link=db.execute('SELECT * FROM pr_links WHERE repo=? AND issue_number=? AND number=?',(repo,number,pr['number'])).fetchone()
                if not link or link['head']!=pr['head'] or link['base']!=pr['base']:raise Refused('pr_link_or_revision_mismatch')
        slot=db.execute('SELECT * FROM slots WHERE session=?',(session,)).fetchone()
        if slot and (slot['repo']!=repo or slot['role']!=role):raise Refused('slot_identity_mismatch')
        if slot and slot['state']!='free':raise Refused('slot_occupied')
        if db.execute("SELECT 1 FROM assignments WHERE repo=? AND role=? AND state!='retired'",(repo,role)).fetchone():raise Refused('role_already_assigned')
        if max_slots is not None and db.execute("SELECT count(*) FROM slots WHERE state!='free'").fetchone()[0]>=max_slots:raise Refused('global_slot_capacity')
        if max_per_role is not None and db.execute("SELECT count(*) FROM slots WHERE state!='free' AND role=?",(role,)).fetchone()[0]>=max_per_role:raise Refused('role_slot_capacity')
        assignment_id=str(uuid.uuid4())
        db.execute('INSERT INTO assignments(id,repo,role,session,revision,issue_numbers,pr,state,provider,generation,attempt,reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,NULL,1,NULL,?,?)',
            (assignment_id,repo,role,session,revision,_json(numbers),_json(pr) if pr else None,'reserved',provider,now,now))
        db.execute('INSERT INTO slots(session,repo,role,state,reason,assignment_id,provider,generation,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(session) DO UPDATE SET state=excluded.state,reason=excluded.reason,assignment_id=excluded.assignment_id,provider=excluded.provider,generation=NULL,bootstrap=0,updated_at=excluded.updated_at',
            (session,repo,role,'reserved','assignment_reserved',assignment_id,provider,None,now,now))
        if role=='build':
            for number in numbers:
                db.execute("UPDATE issues SET stage='build' WHERE repo=? AND number=?",(repo,number))
        result=_assignment(db.execute('SELECT * FROM assignments WHERE id=?',(assignment_id,)).fetchone())
        return _event(db,source_event_id,'reserve',fingerprint,repo,assignment_id,now,dict(after=result),result)


def transition(state, assignment_id, action, *, source_event_id, generation=None, provider=None, reason=None, proof=None, execution=None, now=None):
    """Exact-generation mutations. Proofs are supplied only by trusted verifiers.

    handoff/retired require verified=True, matching assignment_id/generation and
    kind verified_handoff, verified_human_wait or verified_release. Retired also
    requires released=True. Migrate requires verified_provider_handover and
    released=True, preserving target and assignment ID while incrementing attempt.
    Unbound retirement uses verified_no_dispatch for never-submitted work, or
    verified_unstarted_attempt after a recorded migration (attempt > 1). Both
    require confirmed release/absence; neither changes issue completion.
    """
    _text(assignment_id,'assignment_id');now=_time(time.time() if now is None else now)
    if action not in {'bind','submitted','working','idle','uncertain','blocked','handoff','retired','migrate'}:raise Refused('invalid_action')
    if generation is not None:_generation(generation)
    elif action not in {'blocked','retired'}:raise Refused('invalid_generation')
    if provider is not None and provider not in PROVIDERS:raise Refused('invalid_provider')
    if reason is not None:_text(reason,'reason')
    if execution is not None:
        if action not in {'working','idle'} or not isinstance(execution,dict) or set(execution)-{'run_id','thread_id','started_at'}:
            raise Refused('invalid_execution_identity')
        try:
            run_id=str(uuid.UUID(execution['run_id']))
            thread=str(uuid.UUID(execution['thread_id'])) if execution.get('thread_id') is not None else None
            started=_time(execution['started_at'])
        except (KeyError,ValueError,TypeError,AttributeError):raise Refused('invalid_execution_identity')
        if run_id!=execution['run_id'] or thread!=execution.get('thread_id') or not int(generation.split('|')[1])<=started<=now:
            raise Refused('invalid_execution_identity')
        execution=dict(run_id=run_id,thread_id=thread,started_at=started)
    request=dict(assignment_id=assignment_id,action=action,generation=generation,provider=provider,reason=reason,proof=proof,execution=execution)
    with _database(state,write=True) as db:
        fingerprint,replay=_replay(db,source_event_id,'transition',request)
        if replay is not None:return replay
        old=_assignment(db.execute('SELECT * FROM assignments WHERE id=?',(assignment_id,)).fetchone())
        if old['state']=='retired':raise Refused('assignment_retired')
        if now<old['updated_at']:raise Refused('stale_transition')
        slot=db.execute('SELECT * FROM slots WHERE session=?',(old['session'],)).fetchone()
        if not slot or slot['assignment_id']!=assignment_id:raise Refused('slot_assignment_mismatch')
        if action=='bind':
            if old['state'] not in {'reserved','blocked'} or old['generation'] is not None or provider is None:raise Refused('assignment_not_bindable')
        elif old['generation']!=generation or slot['generation']!=generation:
            raise Refused('generation_changed')
        if action!='bind' and provider is not None and provider!=old['provider']:raise Refused('provider_changed')
        allowed={'submitted':{'reserved','submitted','uncertain'},'working':{'submitted','working'},'idle':{'working','submitted'},
            'uncertain':{'reserved','submitted'},'blocked':{'reserved','submitted','working','blocked'},
            'handoff':{'submitted','working','blocked'},'retired':LIVE,'migrate':{'blocked','submitted','working','uncertain'}}
        if action!='bind' and old['state'] not in allowed[action]:raise Refused('transition_not_allowed')
        if action=='submitted' and old['state']=='uncertain':
            if (not isinstance(proof,dict) or proof.get('verified') is not True
                    or proof.get('kind')!='verified_delivery' or proof.get('assignment_id')!=assignment_id
                    or proof.get('generation')!=generation):raise Refused('verified_delivery_proof_required')
        # Eligibility authorizes delivery; it must not hide an already-running
        # exact-generation process after that execution advances GitHub facts.
        if action=='submitted':
            snapshot=db.execute('SELECT * FROM repositories WHERE repo=?',(old['repo'],)).fetchone()
            if not snapshot or not 0<=now-snapshot['observed_at']<=300:
                raise Refused('snapshot_not_fresh')
            for number in old['issue_numbers']:
                item=db.execute('SELECT * FROM issues WHERE repo=? AND number=?',(old['repo'],number)).fetchone()
                if not item or item['stage']!=old['role'] or item['disposition']=='critical':raise Refused('assignment_target_changed')
                if old['pr']:
                    link=db.execute('SELECT head,base FROM pr_links WHERE repo=? AND issue_number=? AND number=?',(old['repo'],number,old['pr']['number'])).fetchone()
                    if not link or link['head']!=old['pr']['head'] or link['base']!=old['pr']['base']:raise Refused('assignment_pr_revision_changed')
        if action in {'handoff','retired','migrate'}:
            kinds={'verified_provider_handover'} if action=='migrate' else {'verified_handoff','verified_human_wait','verified_release'}
            if action=='retired' and generation is None:
                if old['generation'] is not None or old['state'] not in {'reserved','blocked'}:raise Refused('reservation_was_dispatched')
                if isinstance(proof,dict) and proof.get('kind')=='verified_unstarted_attempt':
                    if old['attempt']<=1:raise Refused('verified_migration_required')
                    kinds={'verified_unstarted_attempt'}
                else:
                    if old['submitted_at'] is not None:raise Refused('reservation_was_dispatched')
                    kinds={'verified_no_dispatch'}
            if (not isinstance(proof,dict) or proof.get('verified') is not True or proof.get('assignment_id')!=assignment_id
                    or proof.get('generation')!=generation or proof.get('kind') not in kinds
                    or action in {'retired','migrate'} and proof.get('released') is not True):
                raise Refused('verified_release_proof_required')
        if execution and old['execution'] and old['state']=='working' and execution['run_id']!=old['execution']['run_id']:
            raise Refused('execution_changed_without_dispatch')
        new_state={'bind':'reserved','idle':'submitted','migrate':'reserved'}.get(action,action)
        slot_state={'bind':'starting','submitted':'starting','working':'working','idle':'retiring','uncertain':'blocked',
                    'blocked':'blocked','handoff':'retiring','retired':'free','migrate':'reserved'}[action]
        new_provider=provider if action=='bind' else old['provider']
        new_generation=generation
        attempt=old['attempt']
        if action=='migrate':new_provider=None;new_generation=None;attempt+=1
        current_execution=execution if execution is not None else None if action in {'bind','submitted','migrate'} else old['execution']
        db.execute('UPDATE assignments SET state=?,provider=?,generation=?,attempt=?,reason=?,updated_at=?,execution=?,submitted_at=? WHERE id=?',
            (new_state,new_provider,new_generation,attempt,reason,now,_json(current_execution) if current_execution else None,old['submitted_at'] if old['submitted_at'] is not None else now if action=='submitted' else None,assignment_id))
        db.execute('UPDATE slots SET state=?,reason=?,assignment_id=?,provider=?,generation=?,updated_at=?,execution=? WHERE session=?',
            (slot_state,reason or action,None if action=='retired' else assignment_id,
             None if action=='retired' else new_provider,None if action=='retired' else new_generation,now,_json(current_execution) if current_execution and action!='retired' else None,old['session']))
        for number in old['issue_numbers']:
            row=db.execute('SELECT * FROM issues WHERE repo=? AND number=?',(old['repo'],number)).fetchone()
            if not row or row['stage']!=old['role'] or row['disposition']=='critical':continue
            if action=='working':
                if row['disposition'] not in {'pending','working'}:continue
                if old['pr']:
                    link=db.execute('SELECT head,base FROM pr_links WHERE repo=? AND issue_number=? AND number=?',
                                    (old['repo'],number,old['pr']['number'])).fetchone()
                    if not link or link['head']!=old['pr']['head'] or link['base']!=old['pr']['base']:continue
                disposition='working'
            elif action in {'blocked','uncertain'}:disposition='stalled'
            elif (row['disposition']=='working' or action=='migrate'
                    or action in {'bind','submitted'} and old['state'] in {'blocked','uncertain'} and row['reason']==old['reason']):disposition='pending'
            else:continue
            db.execute('UPDATE issues SET disposition=?,reason=? WHERE repo=? AND number=?',(disposition,reason,old['repo'],number))
        result=_assignment(db.execute('SELECT * FROM assignments WHERE id=?',(assignment_id,)).fetchone())
        return _event(db,source_event_id,'transition',fingerprint,old['repo'],assignment_id,now,dict(action=action,before=old,after=result,proof=proof),result)


def observe_slot(state, session, repo, role, generation, slot_state, *, source_event_id,
                 provider=None, reason=None, bootstrap=False, proof=None, now=None):
    """Record configured/free or legacy occupied slots without inventing assignments.

    Bound slots are changed exclusively by transition(). Clearing an occupied
    unassigned slot requires exact-generation verified released proof. Bootstrap
    means a worker actually observed before assignment tracking, not arbitrary
    missing new-dispatch metadata.
    """
    repo=_repo(repo);_text(session,'session',120)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+',session) or role not in ROLES:raise Refused('invalid_role_session')
    if slot_state not in {'free','reserved','starting','working','retiring','blocked'}:raise Refused('invalid_slot_state')
    if provider is not None and provider not in PROVIDERS:raise Refused('invalid_provider')
    if type(bootstrap) is not bool:raise Refused('invalid_bootstrap')
    if generation is not None:_generation(generation)
    if slot_state!='free' and generation is None:raise Refused('occupied_slot_requires_generation')
    if reason is not None:_text(reason,'reason')
    if bootstrap and slot_state!='free':reason='started_before_assignment_tracking'
    now=_time(time.time() if now is None else now)
    request=dict(session=session,repo=repo,role=role,generation=generation,state=slot_state,
                 provider=provider,reason=reason,bootstrap=bootstrap,proof=proof)
    with _database(state,write=True) as db:
        fingerprint,replay=_replay(db,source_event_id,'observe_slot',request)
        if replay is not None:return replay
        old=db.execute('SELECT * FROM slots WHERE session=?',(session,)).fetchone()
        if old:
            if old['repo']!=repo or old['role']!=role:raise Refused('slot_identity_mismatch')
            if old['assignment_id'] is not None:raise Refused('slot_has_assignment')
            if now<old['updated_at']:raise Refused('stale_slot_observation')
            if old['state']!='free':
                if old['generation']!=generation:raise Refused('generation_changed')
                if slot_state=='free' and (not isinstance(proof,dict) or proof.get('verified') is not True
                        or proof.get('released') is not True or proof.get('generation')!=generation
                        or proof.get('kind') not in {'verified_handoff','verified_human_wait','verified_release','verified_provider_handover'}):
                    raise Refused('verified_release_proof_required')
        elif slot_state=='free' and generation is not None:
            raise Refused('free_slot_has_generation')
        db.execute('INSERT INTO slots(session,repo,role,state,reason,assignment_id,provider,generation,created_at,updated_at,bootstrap) VALUES(?,?,?,?,?,NULL,?,?,?,?,?) ON CONFLICT(session) DO UPDATE SET state=excluded.state,reason=excluded.reason,provider=excluded.provider,generation=excluded.generation,updated_at=excluded.updated_at,bootstrap=excluded.bootstrap',
            (session,repo,role,slot_state,reason,None if slot_state=='free' else provider,
             None if slot_state=='free' else generation,old['created_at'] if old else now,now,int(bootstrap and slot_state!='free')))
        result=dict(db.execute('SELECT * FROM slots WHERE session=?',(session,)).fetchone())
        return _event(db,source_event_id,'observe_slot',fingerprint,repo,None,now,dict(before=dict(old) if old else None,after=result),result)


def read_state(state, repo=None):
    """Read local authority only. No external enrichment or implicit writes."""
    repo=_repo(repo) if repo is not None else None
    if not _path(state).exists() and (Path(state)/'workflow.initialized').exists():raise Refused('workflow_database_missing')
    if not _path(state).exists():return {'status':'uninitialized','issues':[],'assignments':[],'slots':[],'repositories':[]}
    with _database(state) as db:
        where=' WHERE repo=?' if repo else '';parameters=(repo,) if repo else ()
        issues=[dict(row) for row in db.execute('SELECT * FROM issues'+where+' ORDER BY repo,number',parameters)]
        for item in issues:
            item['prs']=[dict(row) for row in db.execute('SELECT number,head,base FROM pr_links WHERE repo=? AND issue_number=? ORDER BY number',(item['repo'],item['number']))]
        return dict(status='ok',issues=issues,
            assignments=[_assignment(row) for row in db.execute('SELECT * FROM assignments'+where+' ORDER BY created_at,id',parameters)],
            slots=[{**dict(row),'execution':json.loads(row['execution']) if row['execution'] else None} for row in db.execute('SELECT * FROM slots'+where+' ORDER BY session',parameters)],
            repositories=[dict(row) for row in db.execute('SELECT * FROM repositories'+where+' ORDER BY repo',parameters)])


def history(state, *, repo=None, after=0, limit=100, pending_outbox=False):
    if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=1000:raise Refused('invalid_page')
    if not _path(state).exists() and (Path(state)/'workflow.initialized').exists():raise Refused('workflow_database_missing')
    if not _path(state).exists():return []
    params=[after];conditions=['e.id>?']
    if repo is not None:conditions.append('e.repo=?');params.append(_repo(repo))
    if pending_outbox:conditions.append('o.delivered_at IS NULL')
    params.append(limit)
    with _database(state) as db:
        rows=db.execute('SELECT e.* FROM events e JOIN outbox o ON o.event_id=e.id WHERE '+' AND '.join(conditions)+' ORDER BY e.id LIMIT ?',params)
        result=[]
        for row in rows:
            item=dict(row);item['payload']=json.loads(item['payload']);item['result']=json.loads(item['result']);result.append(item)
        return result


def ack_outbox(state, through, *, now=None):
    if type(through) is not int or through<0:raise Refused('invalid_outbox_cursor')
    with _database(state,write=True) as db:
        db.execute('UPDATE outbox SET delivered_at=? WHERE event_id<=? AND delivered_at IS NULL',(_time(time.time() if now is None else now),through))
