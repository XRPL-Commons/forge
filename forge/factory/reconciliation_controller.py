"""Fresh GitHub verification before releasing imported migration boundaries."""
import json
import time
import uuid
from urllib.parse import quote

from ..store.db import StoreError
from ..store.models import Command, TransitionError
from ..store.queries import _limit
from ..store.reconciliation import Reconciliation, authorization_in
from ..store.transitions import manifest_hash
from .config import policy, validate
from .dispatch_controller import DispatchController
from .github import GitHubUnavailable
from .merge_controller import candidate_snapshot
from .ownership import ownership


class ReconciliationController:
    def __init__(self,store,github,*,config,admission,observations=None,requirements=None,clock=time.time):
        self.store,self.github,self.clock=store,github,clock
        self.config,self.admission=validate(config),admission
        self.observations,self.requirements=observations,requirements
        self.commands=Reconciliation(store,clock=clock)

    def step(self,unit_id):
        with self.store.read_snapshot() as db:
            authorization=authorization_in(db)
            gate=db.execute("SELECT * FROM unit_gates WHERE unit_id=? AND code='legacy_reconciliation' AND resolved_at IS NULL",(unit_id,)).fetchone()
            if not gate:return {'unit_id':unit_id,'reconciled':True}
            if not authorization:return {'unit_id':unit_id,'pending':'migration_authorization_required'}
        with self.store.read_snapshot() as db:
            member=db.execute('SELECT issue_id FROM unit_issues WHERE unit_id=? AND left_at IS NULL LIMIT 1',(unit_id,)).fetchone()
        if member:
            from .operator_reconciliation import ImportedOperatorController
            interrupted=ImportedOperatorController(self.store,self.github,clock=self.clock).step(member[0])
            if interrupted.get('interrupted') or interrupted.get('pending'):return {'unit_id':unit_id,**interrupted}
        snapshot=DispatchController(self.store,None,config=self.config,prepare=None,admission=self.admission,clock=self.clock).snapshot(unit_id)
        unit,member,issue=snapshot['unit'],snapshot['member'],snapshot['issue']
        key=str(unit['repository_id'])+':'+str(issue['github_number'])
        blocked=lambda code:dict(unit_id=unit_id,issue_id=issue['id'],key=key,pending=code)
        if unit['state'] in {'merged','closed','dissolved'}:return blocked('terminal_reconciliation')
        with self.store.read_snapshot() as db:
            from ..store.migration_recovery import pending_in, phase_in, unresolved_results_in
            investigations=pending_in(db,unit_id)
            phase=phase_in(db,unit,investigations)
            if db.execute('SELECT 1 FROM jobs WHERE unit_id=? AND cleanup=0',(unit_id,)).fetchone():return blocked('cleanup_pending')
            if unresolved_results_in(db,unit_id,investigations):return blocked('legacy_result_pending')
            if db.execute("SELECT 1 FROM attempt_failures WHERE unit_id=? AND investigation IN ('pending','incomplete') AND recovery_required=1",(unit_id,)).fetchone():return blocked('legacy_investigation_pending')
            if db.execute("SELECT 1 FROM unit_gates WHERE unit_id=? AND resolved_at IS NULL AND code='recovery_pending' AND json_extract(details_json,'$.source') IS NOT 'state/factory/factory-ledger.json'",(unit_id,)).fetchone():return blocked('legacy_investigation_pending')
            if db.execute("SELECT 1 FROM issue_action_requests WHERE issue_id=? AND status='pending'",(issue['id'],)).fetchone():return blocked('legacy_operator_request_pending')
            if db.execute("SELECT 1 FROM unit_gates WHERE unit_id=? AND code='operator_sync' AND resolved_at IS NULL",(unit_id,)).fetchone():return blocked('legacy_operator_effect_pending')
            if (db.execute("SELECT 1 FROM external_effects WHERE unit_id=? AND status IN ('pending','running','uncertain')",(unit_id,)).fetchone()
                    or db.execute("SELECT 1 FROM merge_requests WHERE unit_id=? AND status IN ('pending','uncertain')",(unit_id,)).fetchone()):return blocked('legacy_external_effect_pending')
        item=dict(repo_id=unit['repository_id'],repo=issue['repository'],number=issue['github_number'],candidate={'pr':snapshot['candidate']['pull_number'],'key':snapshot['candidate']['id']} if snapshot['candidate'] else None,unit_state=unit['state'])
        from ..store.operator_views import ownership_context
        item=ownership_context(self.store,item,unit['current_candidate_id'])
        remote,requirements=self.github.issue(item)
        if (remote.get('number')!=item['number'] or 'pull_request' in remote or not remote.get('updated_at')
                or not isinstance(requirements,str) or not requirements):raise GitHubUnavailable('Migration issue identity is unverified')
        observed_pr=None
        if snapshot['candidate']:
            selected=snapshot['candidate'];observed_pr=self.github.pr(item,selected['pull_number'])
            if observed_pr.get('merged') is True:
                if (observed_pr.get('state')!='closed' or observed_pr.get('number')!=selected['pull_number']
                        or observed_pr.get('head',{}).get('repo',{}).get('id')!=unit['repository_id']
                        or observed_pr.get('base',{}).get('repo',{}).get('id')!=unit['repository_id']
                        or observed_pr.get('head',{}).get('sha')!=selected['head_oid']
                        or observed_pr.get('base',{}).get('sha')!=selected['base_oid']
                        or not isinstance(observed_pr.get('merge_commit_sha'),str) or not observed_pr['merge_commit_sha']):
                    return blocked('legacy_merge_receipt_unverified')
                proof=dict(verified=True,merged=True,merge_sha=observed_pr['merge_commit_sha'],
                    pull_number=selected['pull_number'],head_oid=selected['head_oid'],base_oid=selected['base_oid'],
                    observed_at=self.clock(),source_manifest=authorization['source_manifest'])
                outcome=self.commands.confirm_merged(Command(str(uuid.uuid4()),authorization['actor_id'],unit_id,unit['version']),
                    selected['id'],merge_sha=observed_pr['merge_commit_sha'],evidence=proof)
                return {**outcome,'unit_id':unit_id,'key':key,'reconciled':True,'merged':True}
        if remote.get('state')=='closed':
            if remote.get('state_reason') not in {'completed','not_planned','duplicate'}:
                return blocked('legacy_closure_provenance_unknown')
            proof=dict(verified=True,observed_at=self.clock(),issue=remote,issue_version=issue['version'])
            if snapshot['candidate']:
                selected=snapshot['candidate'];pr=observed_pr
                if (pr.get('number')!=selected['pull_number'] or pr.get('merged') is not False
                        or pr.get('head',{}).get('sha')!=selected['head_oid']
                        or pr.get('head',{}).get('repo',{}).get('id')!=unit['repository_id']
                        or pr.get('base',{}).get('repo',{}).get('id')!=unit['repository_id']):
                    return blocked('legacy_closure_candidate_unverified')
                proof.update(candidate_id=selected['id'],pull_number=selected['pull_number'],head_oid=selected['head_oid'],merged=False)
            outcome=self.commands.close(Command(str(uuid.uuid4()),authorization['actor_id'],unit_id,unit['version']),gate_id=gate['id'],proof=proof)
            return {**outcome,'unit_id':unit_id,'key':key}
        if remote.get('state')!='open':return blocked('legacy_issue_state_unknown')
        if self.admission(snapshot) is not True:return blocked('repository_setup')
        if ownership(remote,item,item['repo'],self.github.pulls(item)):return blocked('ownership_conflict')
        target=self.github.target(item)
        if target!=unit['base_branch'] or not self.github.protections(item,{'base_ref':target}):return blocked('target_protection_changed')
        candidate_proof,checks,invalidation=None,{},None
        if snapshot['candidate']:
            selected=snapshot['candidate']; candidate_item=candidate_snapshot(self.store,unit_id)
            pr=observed_pr
            if (pr.get('number')!=selected['pull_number'] or pr.get('state') not in {'open','closed'} or pr.get('merged') is not False
                    or not isinstance(pr.get('head',{}).get('sha'),str) or not pr['head']['sha']
                    or not isinstance(pr.get('base',{}).get('sha'),str) or not pr['base']['sha']
                    or pr.get('base',{}).get('ref')!=unit['base_branch']
                    or pr.get('head',{}).get('repo',{}).get('id')!=unit['repository_id'] or pr.get('base',{}).get('repo',{}).get('id')!=unit['repository_id']):
                return blocked('legacy_candidate_unverified')
            base=self.github.call('controller',item,'GET','/branches/'+quote(target,safe='')).get('commit',{}).get('sha')
            if not isinstance(base,str) or not base:return blocked('legacy_candidate_base_unverified')
            current_policy=manifest_hash(policy(self.config,unit['repository_id'],repository_name=item['repo']))
            reason=('legacy_retry' if phase=='building' else
                'requirements_changed' if selected['requirements_hash']!=requirements else
                'policy_changed' if selected['policy_hash']!=current_policy else
                'candidate_changed' if pr['head']['sha']!=selected['head_oid'] or pr['head'].get('ref')!=selected['branch'] or pr['state']!='open' or pr.get('draft') is not False else
                'base_changed' if pr['base']['sha']!=selected['base_oid'] or base!=selected['base_oid'] else None)
            candidate_proof={key:selected[key] for key in ('pull_number','branch','head_oid','base_oid','policy_hash')}
            if reason:
                invalidation=dict(reason=reason,pr=pr,target_head=base,policy_hash=current_policy)
            else:
                invalidation=None
            required={'reviewing':(), 'auditing':('review',), 'merging':('review','audit')}.get(phase or unit['state'])
            if required is None and invalidation is None:return blocked('legacy_candidate_phase_unknown')
            if invalidation is not None:required=()
            runs=self.github.pages('controller',item,f"/commits/{selected['head_oid']}/check-runs?filter=all",'check_runs') if required else []
            if len(runs)>100:raise GitHubUnavailable('Migration technical checks exceed their bound')
            for role in required:
                accepted=candidate_item['evidence'].get(role,{})
                app=self.github.connection(role)['app_id']
                own=[row for row in runs if row.get('name')=='forge/'+role and row.get('app',{}).get('id')==app]
                if not own:return blocked('legacy_'+role+'_check_unavailable')
                check=max(own,key=lambda row:row['id'])
                if (check.get('id')!=accepted.get('check_id') or check.get('head_sha')!=selected['head_oid']
                        or check.get('status')!='completed' or check.get('conclusion')!='success'):
                    return blocked('legacy_'+role+'_check_changed')
                checks[role]=dict(check_id=check['id'],app_id=app,head_oid=selected['head_oid'],status='completed',conclusion='success')
            candidate_proof={key:selected[key] for key in ('pull_number','branch','head_oid','base_oid','policy_hash')}
        observed={key:remote[key] for key in ('number','state','title','updated_at')}
        observed.update(body=remote.get('body') or '',labels=[r['name'] if isinstance(r,dict) else r for r in remote.get('labels',[])],
            assignees=[r['login'] if isinstance(r,dict) else r for r in remote.get('assignees',[])],
            assignee_types={r['login']:r['type'] for r in remote.get('assignees',[]) if isinstance(r,dict) and r.get('type')})
        proof=dict(verified=True,admission=True,ownership_clear=True,observed_at=self.clock(),issue=observed,requirements=requirements,
            issue_id=issue['id'],issue_version=issue['version'],requirements_revision_id=member['requirements_revision_id'],
            candidate_id=unit['current_candidate_id'],candidate=candidate_proof,checks=checks,invalidation=invalidation,investigations=investigations)
        outcome=self.commands.accept(Command(str(uuid.uuid4()),authorization['actor_id'],unit_id,unit['version']),gate_id=gate['id'],proof=proof)
        # Ordinary observers now consume the canonical digest without destroying
        # a same-commit imported candidate. All other gates stay independent.
        if self.observations:self.observations.step(unit_id)
        if self.requirements:self.requirements.observe(unit_id)
        return {**outcome,'unit_id':unit_id,'key':key}

    def cycle(self,*,after=None,limit=50):
        limit=_limit(limit)
        with self.store.read_snapshot() as db:
            if authorization_in(db) is None:return {'items':[],'next_cursor':None}
            rows=db.execute("SELECT DISTINCT unit_id FROM unit_gates WHERE code='legacy_reconciliation' AND resolved_at IS NULL AND unit_id>? ORDER BY unit_id LIMIT ?",(after or '',limit+1)).fetchall()
        items=[]
        for row in rows[:limit]:
            try:items.append(self.step(row['unit_id']))
            except (GitHubUnavailable,TransitionError,StoreError,OSError) as error:
                items.append({'unit_id':row['unit_id'],'error':getattr(error,'code',type(error).__name__)})
        return {'items':items,'next_cursor':rows[limit-1]['unit_id'] if len(rows)>limit else None}
