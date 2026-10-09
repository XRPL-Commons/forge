"""Bounded public pipeline projections from normalized SQLite rows.

These are HTTP/UI views, never a writable legacy ledger. Bodies, diagnostics and
worker artifacts are fetched only through detail/output requests.
"""
import json
import math
import time

from ..store.artifacts import Artifacts, ArtifactExpired
from ..store.queries import Queries
from ..store.operator_views import reviewer_request_in
from ..store.transitions import manifest_hash
from .config import validate
from .live_output import summarize
from .status_projection import operator_status


LEGACY_STAGE = {'queued': 'ready', 'building': 'ready', 'integrating': 'ready', 'reviewing': 'built',
                'auditing': 'reviewed', 'merging': 'audited', 'merged': 'merged', 'closed': 'cancelled', 'dissolved': 'cancelled'}


def action_revision(db, issue_id):
    issue = Queries._issue(db, issue_id)
    lifecycle = issue['lifecycle']
    return manifest_hash({'issue': issue_id, 'issue_version': issue['version'],
                          'unit': lifecycle['unit_id'], 'unit_version': lifecycle['unit_version'],
                          'candidate': lifecycle['candidate_id'], 'reason': lifecycle['reason_code']})


def available_actions(issue, unit, *, cleanup=False, pending=False):
    if pending or issue.get('body_truncated') or issue['github_state'] != 'open':
        return []
    life = issue['lifecycle']
    if life['unit_id'] is None:
        return ['queue'] if life['state'] == 'untracked' else []
    reason = life['reason_code']
    if reason in {'operator_pause', 'operator_backlog', 'label_hold'}:
        return [] if cleanup else ['resume'] if reason != 'operator_backlog' else ['queue']
    if reason in {'budget_exhausted', 'approach_required', 'decision_required'}:
        return [] if cleanup else ['retry']
    if reason in {'environment', 'repository_setup', 'observation_unavailable'}:
        return [] if cleanup else ['recheck']
    if reason == 'approval_required':
        return ['assign_reviewer', 'pause']
    if reason == 'verification_human':
        return ['pause']  # Explicit verification has its own attributed endpoint.
    if reason in {'membership_decision', 'legacy_reconciliation', 'external_change'}:
        return []
    return ['pause'] if cleanup else ['pause', 'backlog']


def _event(row, fields=('at','kind','reason')):
    payload=json.loads(row['payload_json'])
    source=payload.get('legacy',{}) if row['kind']=='legacy_event' else {**payload,'at':row['at'],'kind':row['kind']}
    return {key:source[key] for key in fields if key in source}


# Actual terminal event time, including normalized imported events. Observation
# timestamps can advance after completion and cannot reorder completed cards.
COMPLETION_SQL="COALESCE((SELECT e.at FROM events e WHERE e.unit_id=u.id AND (e.kind IN ('merged','closed','dissolved') OR (e.kind='legacy_event' AND json_extract(e.payload_json,'$.legacy_kind') IN ('merged','cancelled'))) ORDER BY e.sequence DESC LIMIT 1),0)"


class PipelineViews:
    def __init__(self, store, *, repositories=None, runtime=None, clock=time.time, live=None):
        # `live(job)` reads a running worker's scrubbed tail (live_output.py).
        self.store, self.clock, self.live = store, clock, live
        self.repositories = repositories
        self.runtime = runtime or (lambda: validate({}))

    def _config(self):
        return self.runtime() if callable(self.runtime) else self.runtime

    def _repositories(self, db):
        if self.repositories is not None:
            return self.repositories() if callable(self.repositories) else self.repositories
        return [dict(id=row['id'], name=row['full_name'], enabled=bool(row['enabled']),
                     setup_url=f"/settings/repositories/{row['id']}?setup=1") for row in db.execute('SELECT * FROM repositories ORDER BY id')]

    @staticmethod
    def _actions(db, issue_id=None):
        where = 'WHERE issue_id=?' if issue_id else ''
        rows = db.execute('SELECT id,issue_id,status,message,created_at FROM issue_action_requests '+where+
                          " ORDER BY (status='pending') DESC,created_at DESC,id DESC LIMIT 50", (issue_id,) if issue_id else ()).fetchall()
        return [dict(id=row['id'], key=row['issue_id'], status=row['status'], message=row['message'], at=row['created_at']) for row in rows]

    def _collections(self, db, issue, unit_id, candidate, *, detail, config):
        numbers={row[0] for row in db.execute('SELECT issue_number FROM revision_display_dependencies WHERE revision_id=? ORDER BY issue_number LIMIT 101',(issue['current_revision_id'],))}
        numbers.update(row[0] for row in db.execute('SELECT i.github_number FROM issue_dependencies d JOIN issues i ON i.id=d.prerequisite_issue_id WHERE d.issue_id=? AND d.requirements_revision_id=? ORDER BY i.github_number LIMIT 101',(issue['id'],issue['current_revision_id'])))
        if unit_id:
            source=db.execute('SELECT d.requirements_body,p.source_content_hash FROM dependency_observations d JOIN revision_presentation p ON p.revision_id=? WHERE d.unit_id=?',(issue['current_revision_id'],unit_id)).fetchone()
            if source and source['requirements_body']==source['source_content_hash']:
                numbers.update(json.loads(row[0]) for row in db.execute("SELECT value_json FROM dependency_observation_members WHERE unit_id=? AND kind='declared' ORDER BY position LIMIT 101",(unit_id,)))
            if detail:
                for gate in db.execute("SELECT details_json FROM unit_gates WHERE unit_id=? AND code='dependency' AND resolved_at IS NULL LIMIT 100",(unit_id,)):
                    value=json.loads(gate[0])
                    if value.get('requirements')==issue.get('requirements'):numbers.update(value.get('issues',[]))
        # Related PRs are indexed when repository observations are ingested.
        pulls=[]
        for source in db.execute('SELECT p.github_number AS number,p.title,l.forge_attempt FROM inventory_pull_issue_links l JOIN inventory_pull_summaries p ON p.repository_id=l.repository_id AND p.github_number=l.pull_number WHERE l.repository_id=? AND l.issue_number=? ORDER BY p.github_number DESC LIMIT 200',(issue['repository_id'],issue['github_number'])):
            kind='candidate' if candidate and source['number']==candidate['pull_number'] else 'forge_attempt' if source['forge_attempt'] else 'external'
            pulls.append({'number':source['number'],'title':source['title'],'kind':kind})
        if candidate and not any(row['number']==candidate['pull_number'] for row in pulls):
            pulls.append({'number':candidate['pull_number'],'title':'Forge pull request','kind':'candidate'})
        reviewers=[];seen=set()
        if detail:
            sources=[(dict(json.loads(row['details_json']),login=row['login']),row['source']) for row in db.execute("SELECT login,source,details_json FROM inventory_collaborators WHERE repository_id=? ORDER BY CASE source WHEN 'collaborator' THEN 0 ELSE 1 END,login COLLATE NOCASE LIMIT 200",(issue['repository_id'],))]
            sources.extend(({'login':login},'configured') for login in config.get('human_reviewers',[]))
            for value,source in sources:
                if value['login'].lower() not in seen:
                    seen.add(value['login'].lower());reviewers.append({'login':value['login'],'name':value.get('name') or '', 'source':source})
        return sorted(numbers),pulls,reviewers

    def _row(self, db, issue_id, *, detail=False, config):
        issue = Queries._issue(db, issue_id, include_body=detail)
        life = issue['lifecycle']
        unit_id = life['unit_id'] or life['historical_unit_id']
        unit = dict(db.execute('SELECT * FROM units WHERE id=?', (unit_id,)).fetchone()) if unit_id else None
        jobs = db.execute("SELECT id,role,phase,outcome,cleanup,fenced,created_at FROM jobs WHERE unit_id=? "
                          'ORDER BY created_at DESC,id DESC LIMIT 6', (unit_id,)).fetchall() if unit_id else []
        active = next((job for job in jobs if not job['cleanup'] and not job['fenced'] and job['outcome'] is None), None)
        cleanup = bool(unit_id and db.execute('SELECT 1 FROM jobs WHERE unit_id=? AND cleanup=0 LIMIT 1', (unit_id,)).fetchone())
        pending = db.execute("SELECT 1 FROM issue_action_requests WHERE issue_id=? AND status='pending' LIMIT 1", (issue_id,)).fetchone() is not None
        display_candidate_id=life['candidate_id'] or (unit['current_candidate_id'] if unit else None)
        candidate = db.execute('SELECT * FROM candidates WHERE id=?', (display_candidate_id,)).fetchone() if display_candidate_id else None
        dependency_numbers,pull_requests,reviewers=self._collections(db,issue,unit_id,candidate,detail=detail,config=config)
        operator = operator_status(issue, bool(active))
        reason, status = life['reason_code'], operator['code']
        blocker = life['current_blocker']
        details = json.loads(blocker['details_json']) if blocker else {}
        context=db.execute('SELECT * FROM unit_context WHERE unit_id=?',(unit_id,)).fetchone() if unit_id else None
        gates={}
        if unit_id:
            for gate in db.execute('SELECT * FROM unit_gates WHERE unit_id=? AND resolved_at IS NULL AND (candidate_id IS NULL OR candidate_id=?) ORDER BY created_at,id LIMIT 100',(unit_id,life['candidate_id'])):
                value=json.loads(gate['details_json']);gates[gate['code']]=(dict(gate),value.get('value',value))
        decision=gates.get('decision_required',(None,None))[1]
        verification=gates.get('verification_human') or gates.get('verification_ci')
        environment_block=gates.get('environment',(None,{}))[1] or {}
        merge_block=gates.get('merge_checks',(None,{}))[1] or {}
        approval_gate=gates.get('approval_required',(None,{}))[1] or {}
        approval=None
        if candidate and (approval_gate or verification and not verification[1].get('ci')):
            approval={'pr':candidate['pull_number'],'reason':approval_gate.get('reason') or (context['reason'] if context else None) or 'Review the pull request changes.'}
        verification_view={**verification[1],'gate_id':verification[0]['id'],'created_at':verification[0]['created_at']} if verification else None
        approvals=[]
        if candidate and unit['state']=='merging':
            approval_rows=db.execute("SELECT details_json FROM candidate_observations WHERE candidate_id=? AND kind='approval' AND complete=1 ORDER BY collection_generation DESC LIMIT 1",(candidate['id'],)).fetchone()
            approvals=json.loads(approval_rows[0]).get('approvals',[]) if approval_rows else approval_gate.get('approvals',[])
        from .contracts import digest
        label_events=[dict(label=r['label'],login=r['login'],at=r['applied_at']) for r in db.execute('SELECT label,login,applied_at FROM issue_label_attributions WHERE issue_id=? AND labels_hash=? ORDER BY position LIMIT 100',(issue_id,digest(sorted(issue['labels']))))]
        held_by=[event for event in label_events if event['label'] in config['hold_labels'] and event['label'] in issue['labels']] or None
        display=db.execute('SELECT source_content_hash FROM revision_presentation WHERE revision_id=?',(issue['current_revision_id'],)).fetchone()
        checked=context['last_observed_at'] if context and ('legacy_reconciliation' in gates or unit['state'] in {'merged','closed','dissolved'}) else issue['observed_at'] if unit else None
        blocked_dependencies=[]
        if unit_id:
            check=db.execute('SELECT facts_json FROM dependency_checks WHERE unit_id=? AND requirements_revision_id=? ORDER BY generation DESC LIMIT 1',(unit_id,issue['current_revision_id'])).fetchone()
            if check:blocked_dependencies=[fact['number'] for fact in json.loads(check[0]) if fact['state']!='closed']
            else:blocked_dependencies=[json.loads(value[0]) for value in db.execute("SELECT value_json FROM dependency_observation_members WHERE unit_id=? AND kind='blocked' ORDER BY position LIMIT 100",(unit_id,))]
        charges = db.execute("SELECT COALESCE(SUM(CASE b.kind WHEN 'charge' THEN 1 ELSE -1 END),0) FROM budget_entries b "
                             'JOIN attempts a ON a.id=b.attempt_id WHERE a.unit_id=? AND a.budget_generation=?',
                             (unit_id, unit['budget_generation'])).fetchone()[0] if unit else 0
        legacy = LEGACY_STAGE.get(life['stage_code'])
        if reason in {'approval_required', 'verification_human', 'verification_ci'}:
            legacy = 'approval_needed'
        elif reason == 'budget_exhausted':
            legacy = 'failed'
        elif reason == 'recovery_pending':
            legacy = 'investigating'
        elif reason:
            legacy = 'hold'
        environment = db.execute('SELECT choice_json,override_json,environment_hold_json FROM unit_environment_observations WHERE unit_id=?', (unit_id,)).fetchone() if unit_id else None
        if environment and environment['environment_hold_json'] and unit['state'] in {'merged','closed','dissolved'}:
            environment_block=json.loads(environment['environment_hold_json'])
        fresh = not issue.get('body_truncated') and issue['observed_at'] is not None and 0 <= self.clock()-issue['observed_at'] < 300
        actions = available_actions(issue, unit, cleanup=cleanup, pending=pending) if fresh else []
        wording = life['display_status']
        summary={'headline':wording,'plain':details.get('summary') or details.get('reason') or wording}
        written=environment_block.get('summary') or (json.loads(context['summary_json']) if context and context['summary_json'] and (decision or reason=='budget_exhausted') else None)
        if isinstance(written,dict) and written.get('headline'):
            from .contracts import clamp,plain_text,HEADLINE_LIMIT
            summary={'headline':clamp(written['headline'],HEADLINE_LIMIT),'plain':plain_text(written.get('plain'))}
        row = dict(key=issue_id, repo=issue['repository'], repo_id=issue['repository_id'], number=issue['github_number'],
            title=issue['title'], content_hash=display[0] if display else issue['content_hash'], body_truncated=bool(issue.get('body_truncated')),
            state=issue['github_state'], state_reason=issue['state_reason'], updated_at=issue['remote_updated_at'],
            labels=issue['labels'], assignees=issue['assignees'], assignee_types=issue['assignee_types'], status=status, reason_code=reason, operator_status=operator,
            stage_code=life['stage_code'], lifecycle=life, ledger_state=legacy, explanation=wording,
            summary=summary,
            actions=actions, closable=fresh and issue['github_state']=='open' and not pending,
            setup_needed=reason=='repository_setup', environment_blocker=environment_block.get('detail') or environment_block.get('summary'),
            decision_request=decision,
            approval=approval,
            verification_request={**verification_view,'can_confirm':fresh and reason=='verification_human'} if verification_view else None,
            approvals=approvals, last_checked=checked, merge_blocker={'reason':merge_block.get('reason') or wording} if merge_block else None,
            environment={'choice':json.loads(environment['choice_json'] or 'null') if environment else None,
                         'override':json.loads(environment['override_json'] or 'null') if environment else None,
                         'full_available':'full' in config.get('environment_tiers',{}), 'editable':fresh and not cleanup and not pending and issue['github_state']=='open'},
            pull_requests=pull_requests,
            dependencies=dependency_numbers, fresh=fresh, revision=action_revision(db,issue_id), setup_url=f"/settings/repositories/{issue['repository_id']}?setup=1",
            active=active['id'] if active else None, attempts={'failed':charges,'limit':3} if charges else None, held_by=held_by)
        if detail:
            row['body'] = issue['body']
            if label_events:row['label_events']=label_events
            events = db.execute('SELECT at,kind,payload_json FROM events WHERE issue_id=? OR unit_id=? ORDER BY sequence DESC LIMIT 15', (issue_id,unit_id)).fetchall()
            row['history'] = [_event(e) for e in reversed(events)]
            evidence = db.execute("SELECT kind,verdict,details_json FROM candidate_evidence WHERE candidate_id=? AND kind IN ('review','audit') ORDER BY observed_at DESC LIMIT 2", (display_candidate_id,)).fetchall() if candidate else []
            row['reviews'] = []
            for entry in evidence:
                report=json.loads(entry['details_json']);report=report.get('report',report)
                row['reviews'].append(dict(role=entry['kind'],outcome=entry['verdict'],summary=report.get('summary','')[:1000],
                    human_required=report.get('human_required')=='required',human_reason=str(report.get('human_reason') or '')[:2000] or None))
            row.update(reviewers=reviewers,
                       default_reviewers=config.get('human_reviewers',[]),reviewers_requested=reviewer_request_in(db,display_candidate_id))
        row['_jobs']=[dict(job) for job in jobs]
        row['_unit']=unit
        row['_last_summary']=json.loads(context['summary_json']) if context and context['summary_json'] else None
        row['_blocked_dependencies']=blocked_dependencies
        return row

    @staticmethod
    def _public(row):
        return {key:value for key,value in row.items() if not key.startswith('_')}

    def issues(self, *, repo_id=None, state='open', query='', page=1, key=None):
        if state not in {'open','all','completed','dropped','backlog','queued','working','attention','waiting','blocked'} or type(page) is not int or not 1<=page<=10000 or not isinstance(query,str) or len(query)>200:
            raise ValueError('Invalid issue view')
        config=self._config()
        with self.store.read_snapshot() as db:
            repositories=self._repositories(db)
            scopes=[]
            for repo in repositories:
                observed=db.execute("SELECT fetched_at,complete,error_code FROM repository_observations WHERE repository_id=? AND kind='inventory' ORDER BY collection_generation DESC LIMIT 1",(repo['id'],)).fetchone()
                scopes.append(dict(id=repo['id'],name=repo['name'],complete=bool(observed and observed['complete']),
                    fresh=bool(observed and not observed['error_code'] and 0<=self.clock()-observed['fetched_at']<300),
                    at=observed['fetched_at'] if observed else None,error=observed['error_code'] if observed else None))
            if key is not None:
                row=self._row(db,key,detail=True,config=config)
                return dict(issue=self._public(row),repositories=scopes,actions=self._actions(db,key))
            clauses,parameters=['r.enabled=1'],[]
            if repo_id is not None:
                clauses.append('i.repository_id=?');parameters.append(repo_id)
            if state=='open':clauses.append("i.github_state='open'")
            elif state!='all':clauses.append('x.public_status=?');parameters.append(state)
            if query:
                clauses.append("instr(lower(p.title||' '||r.full_name||' #'||i.github_number),lower(?))>0");parameters.append(query)
            source=(' FROM issues i JOIN issue_revisions p ON p.id=i.current_revision_id JOIN repositories r ON r.id=i.repository_id '
                    'JOIN public_issue_index x ON x.id=i.id WHERE '+' AND '.join(clauses))
            total=db.execute('SELECT COUNT(*)'+source,parameters).fetchone()[0]
            ids=db.execute('SELECT i.id'+source+' ORDER BY r.full_name COLLATE NOCASE,i.github_number LIMIT 50 OFFSET ?',(*parameters,(page-1)*50)).fetchall()
            rows=[self._public(self._row(db,row[0],config=config)) for row in ids]
            return dict(issues=rows,total=total,page=page,pages=max(1,math.ceil(total/50)),repositories=scopes,
                        complete=all(scope['complete'] and scope['fresh'] for scope in scopes if repo_id is None or scope['id']==repo_id),actions=self._actions(db))

    def job(self, job_id):
        with self.store.read_snapshot() as db:
            row=db.execute('SELECT j.*,COALESCE(l.slot,p.slot) AS slot,COALESCE(l.provider,p.provider) AS provider,COALESCE(l.provider_id,p.provider_id) AS provider_id,COALESCE(l.model,p.model) AS model,COALESCE(l.deadline,p.deadline) AS deadline,l.branch,COALESCE(e.captured_at,p.observed_at) AS captured_at,p.started_at,p.source_phase,p.source_fenced,u.repository_id,r.full_name,'
                '(SELECT issue_id FROM unit_issues WHERE unit_id=j.unit_id ORDER BY joined_at LIMIT 1) AS issue_key '
                'FROM jobs j LEFT JOIN job_launches l ON l.job_id=j.id LEFT JOIN job_execution_state e ON e.job_id=j.id '
                'LEFT JOIN job_public_metadata p ON p.job_id=j.id JOIN units u ON u.id=j.unit_id JOIN repositories r ON r.id=u.repository_id WHERE j.id=?',(job_id,)).fetchone()
            if row is None:raise KeyError(job_id)
            issue=db.execute('SELECT github_number FROM issues WHERE id=?',(row['issue_key'],)).fetchone()
            return {**{k:row[k] for k in ('id','role','slot','provider','provider_id','model','phase','outcome','created_at','deadline','branch')},
                    'phase':row['source_phase'] or row['phase'],'execution_phase':row['phase'],
                    'cleanup':bool(row['cleanup']),'fenced':bool(row['source_fenced'] if row['source_fenced'] is not None else row['fenced']),
                    'execution_fenced':bool(row['fenced']),'issue_key':row['issue_key'],
                    'repo_id':row['repository_id'],'repo':row['full_name'],'issue_number':issue[0],
                    'started_at':row['started_at'],'observed_at':row['captured_at'],'last_observed':row['captured_at']}

    def output(self, job_id):
        self.job(job_id)
        with self.store.read_snapshot() as db:
            rows=db.execute("SELECT sequence,media_type,raw_size FROM job_artifacts WHERE job_id=? AND kind='logs' ORDER BY sequence LIMIT 17",(job_id,)).fetchall()
            launch=db.execute('SELECT j.phase,j.outcome,j.cleanup,l.slot,l.runner_config_json,e.container_id,e.captured_at FROM jobs j '
                              'LEFT JOIN job_launches l ON l.job_id=j.id LEFT JOIN job_execution_state e ON e.job_id=j.id WHERE j.id=?',(job_id,)).fetchone()
        if not rows:return self._live(job_id,launch)
        if len(rows)>16 or sum(r['raw_size'] for r in rows)>16*1024*1024:
            raise ValueError('Stored log exceeds the bounded output display limit')
        try:
            raw=b''.join(Artifacts(self.store).read(job_id,'logs',row['sequence']).content for row in rows)
            value=json.loads(raw) if rows[0]['media_type']=='application/json' else raw.decode('utf-8',errors='replace')
            if not isinstance(value,str):raise ValueError('Stored log has an invalid format')
            return {'logs':value,'live':False,**summarize(value)}
        except ArtifactExpired as error:return {'logs':'','expired_at':error.expired_at}

    def _live(self, job_id, launch):
        """Until capture, a running job's output is its container's scrubbed tail."""
        running=(launch is not None and launch['runner_config_json'] and launch['outcome'] is None
                 and not launch['cleanup'] and launch['captured_at'] is None)
        if not running or self.live is None:return {'logs':''}
        if launch['phase']=='reserved' or launch['phase']=='starting' and not launch['container_id']:
            return {'logs':'','live':True,'unavailable':'not_started'}
        observed=self.live({'id':job_id,'slot':launch['slot'],'container_id':launch['container_id'],
                            'runner_config':json.loads(launch['runner_config_json'])})
        if 'unavailable' in observed:return {'logs':'','live':True,'unavailable':observed['unavailable']}
        return {'logs':observed['logs'],'live':True,'at':observed['at'],'running':observed.get('running',True),**summarize(observed['logs'])}

    def factory(self, *, section=None, issue=None, limit=None, before=None):
        """Bounded Activity sections and SQL aggregate counters.

        Candidate metadata and recent events belong to cards. Worker reports and
        full histories remain separate detail/artifact reads.
        """
        if section not in (None,'board','completed','runners') or section and issue:
            raise ValueError('Invalid factory view')
        if (limit is not None or before is not None) and section!='completed':raise ValueError('Invalid factory view')
        size=int(limit) if limit is not None else 50 if section=='completed' else 100
        if not 1<=size<=(50 if section=='completed' else 100):raise ValueError('Invalid factory page size')
        config=self._config()
        from ..settings import digest
        from ..store.operations import deployment_views
        deployments={key:dict(name=value['name'],**{field:(value.get('observed') or {}).get(field) for field in ('live','deploy','error')})
                     for key,value in deployment_views(self.store).items()}
        with self.store.read_snapshot() as db:
            repositories=self._repositories(db)
            heartbeat=db.execute("SELECT value FROM store_metadata WHERE key='supervisor:heartbeat'").fetchone()
            receipt=json.loads(heartbeat[0]) if heartbeat else {}
            observation=dict(at=receipt.get('finished_at'),errors=[{'phase':name,'reason':', '.join(value['codes'])} for name,value in receipt.get('errors',{}).items()])
            clauses=['x.unit_id IS NOT NULL'];params=[]
            completed="x.unit_state IN ('merged','closed','dissolved') AND x.issue_state IN ('done','dropped')"
            if issue is not None:clauses.append('x.id=?');params.append(issue)
            elif section=='completed':clauses.append(completed)
            elif section=='runners':clauses.append("x.reason_code='recovery_pending'")
            else:clauses.append("x.current=1 AND COALESCE(x.reason_code,'')!='recovery_pending'")
            if before is not None:
                raw,sep,key=str(before).partition(':')
                try:at=float(raw)
                except ValueError:raise ValueError('Invalid completed cursor') from None
                if not sep or not key or not math.isfinite(at):raise ValueError('Invalid completed cursor')
                clauses.append('('+COMPLETION_SQL+',x.id)<(?,?)');params.extend((at,key))
            rows=db.execute('SELECT x.id,u.updated_at,'+COMPLETION_SQL+' AS completed_at FROM public_issue_index x JOIN units u ON u.id=x.unit_id WHERE '+
                ' AND '.join(clauses)+' ORDER BY '+('completed_at' if section=='completed' else 'u.updated_at')+' DESC,x.id DESC LIMIT ?',(*params,size+1)).fetchall()
            issues=[]
            for selected in rows[:size]:
                row=self._row(db,selected['id'],config=config);unit=row['_unit'];reason=row['reason_code']
                candidate=db.execute('SELECT pull_number,head_oid FROM candidates WHERE id=?',(unit['current_candidate_id'],)).fetchone() if unit['current_candidate_id'] else None
                deployed=failed=None
                delivery=db.execute("SELECT * FROM deployment_observations WHERE candidate_id=? ORDER BY (status='success') DESC,observed_at DESC LIMIT 1",(unit['current_candidate_id'],)).fetchone()
                if delivery:
                    context=json.loads(delivery['details_json'])
                    if delivery['status']=='success':deployed={'commit':delivery['commit_oid'],'at':delivery['observed_at'],'via':context.get('via','github')}
                    elif delivery['status']=='failure':failed={'sha':delivery['commit_oid'],'at':delivery['observed_at'],'run_url':context.get('run_url')}
                events=db.execute('SELECT kind,at,payload_json FROM events WHERE unit_id=? ORDER BY sequence DESC LIMIT 3',(unit['id'],)).fetchall()
                history=[_event(e,('at','kind','provider','model')) for e in reversed(events)]
                if unit['state'] in {'merged','closed','dissolved'} and selected['completed_at'] and not any(e['kind'] in {'merged','cancelled','closed','dissolved'} for e in history):
                    history.insert(0,{'kind':'merged' if unit['state']=='merged' else 'cancelled','at':selected['completed_at']})
                attention={'approval_required':'approve','verification_human':'verify','budget_exhausted':'failed',
                    'approach_required':'approach','decision_required':'question','environment':'env','repository_setup':'env'}.get(reason)
                issues.append({**{key:row[key] for key in ('key','repo_id','repo','number','title','active','lifecycle','status','reason_code','stage_code','operator_status')},
                    'state':row['ledger_state'] or ('cancelled' if unit['state'] in {'closed','dissolved'} else 'merged' if unit['state']=='merged' else 'hold'),
                    'reason':row['explanation'] if reason else None,'failure_count':(row['attempts'] or {}).get('failed',0),
                    'candidate':dict(pr=candidate['pull_number'],head=candidate['head_oid'],merge_commit=unit['merge_sha']) if candidate else None,
                    'last_summary':row['_last_summary'],'history':history,'created_at':unit['created_at'],'last_observed':row['last_checked'],
                    'hold_kind':reason,'merge_gate_kind':reason if reason in {'merge_checks','approval_required','verification_ci','verification_human'} else None,
                    'attention_kind':attention,'closed_as':None if unit['state']=='merged' else row['state_reason'],'reviewers_requested':reviewer_request_in(db,unit['current_candidate_id']),
                    'attempts':[{**{key:job[key] for key in ('id','role','outcome','phase','created_at')},'started_at':None} for job in row['_jobs']],
                    'blocked_dependencies':row['_blocked_dependencies'],'deployed':deployed,'deploy_failed':failed,'completed_at':selected['completed_at'] or None})
            cursor=f"{rows[size-1]['completed_at']!r}:{rows[size-1]['id']}" if len(rows)>size else None
            job_clauses=['(j.cleanup=0 OR j.outcome IS NULL)'];job_params=[]
            runner_filter="(j.role='recovery' OR j.fenced=1 OR j.outcome IS NOT NULL OR NOT EXISTS(SELECT 1 FROM unit_issues m JOIN units u ON u.id=m.unit_id WHERE m.unit_id=j.unit_id AND m.left_at IS NULL AND u.state NOT IN ('merged','closed','dissolved')))"
            if section=='runners':job_clauses.append(runner_filter)
            if issue is not None:
                job_clauses.append('EXISTS(SELECT 1 FROM unit_issues m WHERE m.unit_id=j.unit_id AND m.issue_id=?)');job_params.append(issue)
            job_ids=[] if section=='completed' else [r[0] for r in db.execute('SELECT j.id FROM jobs j WHERE '+' AND '.join(job_clauses)+' ORDER BY j.created_at DESC,j.id DESC LIMIT 100',job_params)]
            slots={r['slot']:r['job_id'] for r in db.execute('SELECT slot,job_id FROM job_launches l JOIN jobs j ON j.id=l.job_id WHERE j.cleanup=0 ORDER BY slot LIMIT 256')}
            counts=db.execute("SELECT COUNT(*) AS total,SUM("+COMPLETION_SQL+">=?) AS day,SUM("+COMPLETION_SQL+">=?) AS week FROM units u WHERE state='merged'",(self.clock()-86400,self.clock()-604800)).fetchone()
            completed_count=db.execute('SELECT COUNT(*) FROM public_issue_index x WHERE '+completed).fetchone()[0]
            runner_count=db.execute('SELECT COUNT(*) FROM jobs j WHERE (j.cleanup=0 OR j.outcome IS NULL) AND '+runner_filter).fetchone()[0]
            running=bool(db.execute("SELECT 1 FROM jobs WHERE phase='running' AND cleanup=0 LIMIT 1").fetchone())
            accounts=[]
            for provider in dict.fromkeys(entry['provider'] for entry in config.get('provider_pool',[])):
                cooldown=db.execute('SELECT * FROM provider_cooldowns WHERE provider=? AND retry_at>? ORDER BY observed_at DESC LIMIT 1',(provider,self.clock())).fetchone()
                ready=config.get('provider_ready');entries=[e['id'] for e in config['provider_pool'] if e['provider']==provider]
                verified=ready is None or bool(set(ready).intersection(entries))
                code=cooldown['reason_code'] if cooldown else None if verified else 'verification_required'
                status={'blocked_auth':'sign_in_expired','blocked_quota':'quota'}.get(code,'unavailable' if code else 'ok')
                accounts.append(dict(provider=provider,name=provider.capitalize(),ready=status=='ok',status=status,reason_code=code,
                    since=cooldown['observed_at'] if cooldown else None,until=cooldown['retry_at'] if cooldown else None,detail=None,
                    waiting_issues=db.execute("SELECT COUNT(DISTINCT unit_id) FROM unit_gates WHERE code='provider_unavailable' AND resolved_at IS NULL").fetchone()[0] if code else 0))
            deployed_day=db.execute("SELECT COUNT(DISTINCT candidate_id) FROM deployment_observations WHERE status='success' AND observed_at>=?",(self.clock()-86400,)).fetchone()[0]
            failed_count=db.execute("SELECT COUNT(DISTINCT candidate_id) FROM deployment_observations d WHERE status='failure' AND NOT EXISTS(SELECT 1 FROM deployment_observations s WHERE s.candidate_id=d.candidate_id AND s.status='success')").fetchone()[0]
            waiting_count=db.execute("SELECT COUNT(*) FROM units u JOIN deployment_profiles p ON p.repository_id=u.repository_id WHERE u.state='merged' AND p.status='confirmed' AND NOT EXISTS(SELECT 1 FROM deployment_observations d WHERE d.candidate_id=u.current_candidate_id AND d.status IN ('success','failure'))").fetchone()[0]
            actions=self._actions(db)
        jobs=[self.job(identifier) for identifier in job_ids]
        if not config['enabled']:
            operator=dict(title='Factory is paused',detail='Enable the factory to resume eligible work.',action_url='/settings',action_label='Review settings')
        elif observation['at'] is None or self.clock()-observation['at']>max(120,3*config['cycle_seconds']):
            operator=dict(title='Supervisor observation is overdue',detail='The last controller cycle has not been confirmed.',action_url='/settings',action_label='Review settings')
        elif any(row['attention_kind'] for row in issues):
            target=next(row for row in issues if row['attention_kind'])
            operator=dict(title='An issue needs your attention',detail=target['reason'],action_url='/issues?issue='+target['key'],action_label='Review issue')
        elif not issues and not runner_count:
            operator=dict(title='Waiting for a ready issue',detail='Queue an issue to start work.',action_url='/issues',action_label='Choose an issue',empty=True)
        else:operator=None
        summary=dict(completed_count=completed_count,runner_count=runner_count,runners_running=running,
            workers=[{key:j.get(key) for key in ('role','phase','cleanup','fenced','outcome','observed_at','last_observed')} for j in jobs],
            merged_day=counts['day'] or 0,merged_week=counts['week'] or 0,merged_total=counts['total'],deployed_day=deployed_day,waiting=waiting_count,deploy_failed=failed_count)
        return dict(active=True,operator=operator,repositories=repositories,observation=observation,actions=actions,
            issues=issues,jobs=jobs,slots=slots,deployments=deployments,runtime=config,runtime_revision=digest(config),
            coding_accounts=accounts,summary=summary,next=cursor)
