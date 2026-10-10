"""Fresh migration reconciliation without spending or resetting delivery budgets."""
import json

from .models import TransitionError
from .remote_time import order as remote_order
from .transitions import Lifecycle, _id, _json, manifest_hash


def authorization_in(db):
    row=db.execute("SELECT value FROM store_metadata WHERE key='migration_reconciliation_authorization'").fetchone()
    if not row: return None
    value=json.loads(row[0])
    operation=db.execute("SELECT * FROM runtime_operations WHERE id=? AND kind='activate' AND stage IN ('selected','complete')",(value.get('operation_id'),)).fetchone()
    if not operation or operation['manifest_digest']!=value.get('source_manifest') or not value.get('actor_id'):
        raise TransitionError('migration reconciliation authorization is invalid')
    return value


class Reconciliation(Lifecycle):
    def close(self, command, *, gate_id, proof):
        """Preserve terminal GitHub provenance without inventing a merge."""
        def change(db,unit):
            authority=authorization_in(db)
            if not authority or authority['actor_id']!=command.actor_id:
                raise TransitionError('migration reconciliation requires the activation authority')
            self._live(unit); self._quiescent(db,unit['id']); member=self._singleton(db,unit)
            gate=db.execute("SELECT * FROM unit_gates WHERE id=? AND unit_id=? AND code='legacy_reconciliation' AND resolved_at IS NULL",(gate_id,unit['id'])).fetchone()
            issue=db.execute('SELECT * FROM issues WHERE id=?',(member['issue_id'],)).fetchone()
            observed=proof.get('issue') or {}; at=proof.get('observed_at')
            if (not gate or proof.get('verified') is not True or type(at) not in (int,float)
                    or not gate['created_at']<=at<=self.clock()+1 or self.clock()-at>60
                    or proof.get('issue_version')!=issue['version'] or observed.get('number')!=issue['github_number']
                    or observed.get('state')!='closed' or observed.get('state_reason') not in {'completed','not_planned','duplicate'}):
                raise TransitionError('migration closure requires fresh exact issue provenance')
            updated = self._remote_time(observed['updated_at'])
            if remote_order(updated) < remote_order(issue['remote_updated_at']):
                raise TransitionError('migration issue observation is older than canonical facts')
            self._prove_not_merged(db,unit,proof)
            db.execute("UPDATE issues SET github_state='closed',state_reason=?,remote_updated_at=?,observed_at=?,version=version+1 WHERE id=?",
                (observed['state_reason'],updated,self.clock(),issue['id']))
            self._event(db,command,'issue_observed',proof,issue_id=issue['id'])
            self._settle(db,command,unit,'closed',close_code='github_'+observed['state_reason'])
            return {'reconciled':True,'closed':True,'close_code':'github_'+observed['state_reason']}
        return self._apply(command,'reconcile_imported_closure',dict(gate_id=gate_id,proof=proof),change)

    def accept(self,command,*,gate_id,proof):
        if (not isinstance(proof,dict) or proof.get('verified') is not True or proof.get('admission') is not True
                or proof.get('ownership_clear') is not True or type(proof.get('observed_at')) not in (float,int)
                or not isinstance(proof.get('requirements'),str) or not proof['requirements']):
            raise TransitionError('migration requires fresh complete controller evidence')
        payload=dict(gate_id=gate_id,proof=proof)
        def change(db,unit):
            authority=authorization_in(db)
            if not authority or authority['actor_id']!=command.actor_id:
                raise TransitionError('migration reconciliation requires the activation authority')
            self._live(unit); self._quiescent(db,unit['id']); member=self._singleton(db,unit)
            gate=db.execute("SELECT * FROM unit_gates WHERE id=? AND unit_id=? AND code='legacy_reconciliation' AND resolved_at IS NULL",(gate_id,unit['id'])).fetchone()
            if gate is None or json.loads(gate['details_json']).get('source')!='state/factory/factory-ledger.json':
                raise TransitionError('gate is not the imported reconciliation boundary')
            if not gate['created_at']<=proof['observed_at']<=self.clock()+1 or self.clock()-proof['observed_at']>60:
                raise TransitionError('migration evidence is stale')
            issue=db.execute('SELECT * FROM issues WHERE id=?',(member['issue_id'],)).fetchone()
            if (proof.get('issue_id')!=issue['id'] or proof.get('issue_version')!=issue['version']
                    or proof.get('requirements_revision_id')!=member['requirements_revision_id']
                    or proof.get('candidate_id')!=unit['current_candidate_id']):
                raise TransitionError('migration evidence quotes obsolete authority')
            from .migration_recovery import pending_in, phase_in, adopt_in, unresolved_results_in
            investigations=pending_in(db,unit['id'])
            if investigations!=proof.get('investigations',[]):raise TransitionError('legacy investigation changed during observation')
            if (db.execute("SELECT 1 FROM unit_gates WHERE unit_id=? AND resolved_at IS NULL AND (code='operator_sync' OR (code='recovery_pending' AND json_extract(details_json,'$.source') IS NOT 'state/factory/factory-ledger.json'))",(unit['id'],)).fetchone()
                    or unresolved_results_in(db,unit['id'],investigations)
                    or db.execute("SELECT 1 FROM attempt_failures WHERE unit_id=? AND investigation IN ('pending','incomplete') AND recovery_required=1",(unit['id'],)).fetchone()
                    or db.execute("SELECT 1 FROM issue_action_requests WHERE issue_id=? AND status='pending'",(issue['id'],)).fetchone()
                    or db.execute("SELECT 1 FROM external_effects WHERE unit_id=? AND status IN ('pending','running','uncertain')",(unit['id'],)).fetchone()
                    or db.execute("SELECT 1 FROM merge_requests WHERE unit_id=? AND status IN ('pending','uncertain')",(unit['id'],)).fetchone()):
                raise TransitionError('legacy investigation or external effect still needs reconciliation')
            observed=proof['issue']
            if observed.get('number')!=issue['github_number'] or observed.get('state')!='open' or 'pull_request' in observed:
                raise TransitionError('migration observation must identify the open issue')
            updated = self._remote_time(observed['updated_at'])
            if remote_order(updated) < remote_order(issue['remote_updated_at']):
                raise TransitionError('migration issue observation is older than canonical facts')
            requirements=proof['requirements']; requirements_hash=manifest_hash(requirements)
            content_hash=manifest_hash([observed['title'],observed.get('body') or ''])
            existing=db.execute('SELECT id FROM issue_revisions WHERE issue_id=? AND content_hash=? AND requirements_hash=?',
                (issue['id'],content_hash,requirements_hash)).fetchone()
            revision=existing['id'] if existing else _id(command.request_id,'reconciled-revision')
            if not existing:
                db.execute('INSERT INTO issue_revisions(id,issue_id,title,body,content_hash,requirements_hash,requirements_json,observed_at) VALUES(?,?,?,?,?,?,?,?)',
                    (revision,issue['id'],observed['title'],observed.get('body') or '',content_hash,requirements_hash,_json(requirements),self.clock()))
                from .presentation import record_revision_display
                record_revision_display(db,revision,observed.get('body') or '',requirements)
            db.execute('UPDATE issues SET current_revision_id=?,remote_updated_at=?,observed_at=?,labels_json=?,assignees_json=?,assignee_types_json=?,version=version+1 WHERE id=?',
                (revision,updated,self.clock(),_json(observed.get('labels',[])),_json(observed.get('assignees',[])),_json(observed.get('assignee_types',{})),issue['id']))
            db.execute('UPDATE unit_issues SET requirements_revision_id=? WHERE id=?',(revision,member['id']))
            candidate_id=unit['current_candidate_id']
            phase=phase_in(db,unit,investigations)
            adopt_in(self,db,command,unit,investigations)
            if phase and phase!='building':
                db.execute('UPDATE units SET state=? WHERE id=?',(phase,unit['id']))
                unit={**unit,'state':phase}
            if candidate_id:
                original=db.execute('SELECT * FROM candidates WHERE id=?',(candidate_id,)).fetchone()
                candidate_proof=proof.get('candidate') or {}
                if any(candidate_proof.get(key)!=original[key] for key in ('pull_number','branch','head_oid','base_oid','policy_hash')):
                    raise TransitionError('migration candidate no longer matches original inputs')
                invalidation=proof.get('invalidation')
                if invalidation:
                    pr=invalidation.get('pr') or {}; target=invalidation.get('target_head'); current_policy=invalidation.get('policy_hash')
                    if (pr.get('number')!=original['pull_number'] or pr.get('merged') is not False
                            or pr.get('state') not in {'open','closed'} or not isinstance(target,str) or not target
                            or not isinstance(current_policy,str) or not current_policy
                            or not isinstance(pr.get('head',{}).get('sha'),str) or not pr['head']['sha']
                            or not isinstance(pr.get('base',{}).get('sha'),str) or not pr['base']['sha']
                            or pr.get('head',{}).get('repo',{}).get('id')!=unit['repository_id']
                            or pr.get('base',{}).get('repo',{}).get('id')!=unit['repository_id']
                            or pr.get('base',{}).get('ref')!=unit['base_branch']):
                        raise TransitionError('migration rework requires fresh exact unmerged PR identity')
                    reason=('legacy_retry' if phase=='building' else
                        'requirements_changed' if original['requirements_hash']!=requirements else
                        'policy_changed' if original['policy_hash']!=current_policy else
                        'candidate_changed' if pr['head']['sha']!=original['head_oid'] or pr['head'].get('ref')!=original['branch'] or pr['state']!='open' or pr.get('draft') is not False else
                        'base_changed' if pr['base']['sha']!=original['base_oid'] or target!=original['base_oid'] else None)
                    if not reason or reason!=invalidation.get('reason'):
                        raise TransitionError('migration rework does not prove changed candidate inputs')
                    self._invalidate(db,command,unit,reason,invalidation)
                    self._resolve(db,command,gate_id,proof)
                    self._event(db,command,'lifecycle_changed',{'migration_reconciled':True,'source_manifest':authority['source_manifest'],'issue_id':issue['id']})
                    return {'reconciled':True,'candidate_id':None,'invalidated':reason,'requirements_revision_id':revision}
                if original['requirements_hash']!=requirements:
                    raise TransitionError('migration candidate requirements changed without rework proof')
                required={'reviewing':(), 'auditing':('review',), 'merging':('review','audit')}.get(unit['state'])
                if required is None: raise TransitionError('legacy candidate phase requires operator reconciliation')
                checks=proof.get('checks') or {}
                fresh_candidate=_id(command.request_id,'reconciled-candidate')
                manifest=dict(requirements_revision_id=revision,base_oid=original['base_oid'],policy_hash=original['policy_hash'],migration_source_candidate_id=candidate_id)
                db.execute('INSERT INTO candidates(id,unit_id,pull_number,branch,head_oid,base_oid,policy_hash,requirements_hash,membership_generation,input_manifest_hash,accepted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (fresh_candidate,unit['id'],original['pull_number'],original['branch'],original['head_oid'],original['base_oid'],original['policy_hash'],requirements_hash,unit['membership_generation'],manifest_hash(manifest),original['accepted_at']))
                for role in required:
                    previous=db.execute("SELECT * FROM candidate_evidence WHERE candidate_id=? AND kind=? AND verdict='pass' ORDER BY observed_at DESC,rowid DESC LIMIT 1",(candidate_id,role)).fetchone()
                    check=checks.get(role) or {}
                    details=json.loads(previous['details_json']) if previous else {}
                    expected_id=details.get('check',{}).get('check_id',details.get('check_id'))
                    if (type(expected_id) is not int or check.get('check_id')!=expected_id or check.get('head_oid')!=original['head_oid']
                            or check.get('conclusion')!='success' or check.get('status')!='completed' or type(check.get('app_id')) is not int):
                        raise TransitionError('migration technical evidence requires fresh exact check identity')
                    db.execute("INSERT INTO candidate_evidence(id,unit_id,candidate_id,attempt_id,kind,verdict,actor_id,observed_at,details_json) VALUES(?,?,?,?,?,'pass',?,?,?)",
                        (_id(command.request_id,role),unit['id'],fresh_candidate,previous['attempt_id'],role,command.actor_id,self.clock(),
                         _json({**details,'migration_source_evidence_id':previous['id'],'migration_verification':check})))
                for old in db.execute('SELECT * FROM unit_gates WHERE unit_id=? AND candidate_id=?',(unit['id'],candidate_id)).fetchall():
                    # Preserve every independent gate and its original resolution;
                    # no hold is implicitly waived by rebinding identical commits.
                    db.execute('INSERT INTO unit_gates(id,unit_id,candidate_id,member_id,subject_key,code,details_json,created_at,resolved_at,resolution) VALUES(?,?,?,NULL,?,?,?,?,?,?)',
                        (_id(command.request_id,'gate:'+old['id']),unit['id'],fresh_candidate,'candidate:'+fresh_candidate,old['code'],old['details_json'],old['created_at'],old['resolved_at'],old['resolution']))
                db.execute('INSERT INTO candidate_reviewer_requests SELECT ?,request_id,actor_id,logins_json,requested_at,issue_assignment FROM candidate_reviewer_requests WHERE candidate_id=?',(fresh_candidate,candidate_id))
                db.execute("UPDATE unit_issues SET state='built' WHERE id=?",(member['id'],))
                db.execute('UPDATE units SET current_candidate_id=? WHERE id=?',(fresh_candidate,unit['id']))
                self._event(db,command,'candidate_selected',{'candidate_id':fresh_candidate,'migration_source_candidate_id':candidate_id,'verified_same_commits':True})
                candidate_id=fresh_candidate
            self._resolve(db,command,gate_id,proof)
            self._event(db,command,'lifecycle_changed',{'migration_reconciled':True,'source_manifest':authority['source_manifest'],'issue_id':issue['id']})
            return {'reconciled':True,'candidate_id':candidate_id,'requirements_revision_id':revision}
        return self._apply(command,'reconcile_imported_unit',payload,change)
