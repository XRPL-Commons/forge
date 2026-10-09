"""Interrupt unauthenticated imported operator work without replaying effects."""
import json
import math
import uuid
from .models import Command, TransitionError, VersionConflict
from .reconciliation import authorization_in
from .runtime import writable
from .transitions import Lifecycle, _json, manifest_hash


class OperatorReconciliation(Lifecycle):
    def interrupt(self, issue_id, *, proof):
        with self.store.write() as db:
            writable(db)
            authority=authorization_in(db)
            if not authority:raise TransitionError('Migration activation authority is required')
            issue=db.execute('SELECT * FROM issues WHERE id=?',(issue_id,)).fetchone()
            observed=proof.get('issue') or {};at=proof.get('observed_at')
            if (not issue or proof.get('verified') is not True or type(at) not in (int,float) or not math.isfinite(at)
                    or not 0<=self.clock()-at<=60 or observed.get('number')!=issue['github_number']
                    or observed.get('state') not in {'open','closed'} or not observed.get('updated_at')):
                raise TransitionError('Fresh exact GitHub issue evidence is required')
            if proof.get('issue_version')!=issue['version']:raise VersionConflict('Issue changed during reconciliation')
            requests=db.execute("SELECT q.*,p.source_status,p.source_message FROM issue_action_requests q JOIN imported_operator_requests p ON p.request_id=q.id LEFT JOIN operator_action_authority a ON a.request_id=q.id WHERE q.issue_id=? AND q.status='pending' AND p.source_manifest=? AND a.request_id IS NULL ORDER BY q.id LIMIT 101",(issue_id,authority['source_manifest'])).fetchall()
            if len(requests)>100:raise TransitionError('Imported actions exceed reconciliation bound')
            unit=db.execute('SELECT u.* FROM units u JOIN unit_issues m ON m.unit_id=u.id WHERE m.issue_id=? AND m.left_at IS NULL',(issue_id,)).fetchone()
            gates=[]
            if unit:
                gates=[row for row in db.execute("SELECT * FROM unit_gates WHERE unit_id=? AND code='operator_sync' AND resolved_at IS NULL",(unit['id'],)) if json.loads(row['details_json']).get('legacy_field')=='operator_sync']
                migration=db.execute("SELECT 1 FROM unit_gates WHERE unit_id=? AND code='legacy_reconciliation' AND resolved_at IS NULL",(unit['id'],)).fetchone()
                if gates and not migration:raise TransitionError('Imported synchronization lacks migration boundary')
            if not requests and not gates:return {'interrupted':False,'issue_id':issue_id}
            command=Command(str(uuid.uuid4()),authority['actor_id'],unit['id'] if unit else None,unit['version'] if unit else None)
            evidence={**proof,'source_manifest':authority['source_manifest'],'operation_id':authority['operation_id']}
            identifiers=[]
            for row in requests:
                original=dict(row); identifiers.append(row['id'])
                db.execute("UPDATE issue_action_requests SET status='rejected',message='Interrupted by migration; review current state and resubmit.' WHERE id=?",(row['id'],))
                db.execute('UPDATE imported_operator_requests SET interrupted_at=?,evidence_json=? WHERE request_id=?',(self.clock(),_json(evidence),row['id']))
                self._event(db,command,'lifecycle_changed',{'migration_interrupted':True,'request_id':row['id'],'original_intent_hash':manifest_hash(original),'source_status':row['source_status'],'evidence':evidence},issue_id=issue_id)
            if unit and unit['state'] not in {'merged','closed','dissolved'}:
                self._fence(db,command)
                for gate in gates:self._resolve(db,command,gate['id'],{'migration_interrupted':True,'evidence':evidence})
                if not db.execute("SELECT 1 FROM unit_gates WHERE unit_id=? AND code='operator_pause' AND resolved_at IS NULL",(unit['id'],)).fetchone():
                    self._open_gate(db,command,'operator_pause',{'reason':'An operator decision was interrupted by migration. Review current state, then resume.','interrupted_requests':identifiers,'interrupted_gates':[g['id'] for g in gates]})
                db.execute('UPDATE units SET version=version+1,updated_at=? WHERE id=?',(self.clock(),unit['id']))
            return {'interrupted':True,'issue_id':issue_id,'requests':identifiers,'gates':[g['id'] for g in gates]}
