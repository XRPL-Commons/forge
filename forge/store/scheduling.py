"""Durable scoped controller decisions; timers never grant work authority."""
import math
import time
import json
import uuid

from .runtime import writable
from .models import Command, TransitionError
from .queries import _limit
from .transitions import Lifecycle, applicable_gates

DEFAULT_RETRY = 30
STALL_SECONDS = 300


def current_in(db, unit, member_id=None):
    """Return only decisions belonging to this exact active assignment."""
    if unit['state'] in {'merged', 'closed', 'dissolved'}:
        return None
    row = db.execute(
        'SELECT n.* FROM next_actions n WHERE n.unit_id=? AND n.unit_version=? '
        'AND n.fencing_generation=? AND n.membership_generation=? '
        'AND (n.candidate_id IS NULL OR n.candidate_id IS ?) '
        'AND (n.member_id IS NULL OR n.member_id=?) '
        'AND (n.job_id IS NULL OR EXISTS(SELECT 1 FROM jobs j JOIN attempts assignment ON assignment.id=j.attempt_id JOIN units assigned_unit ON assigned_unit.id=j.unit_id WHERE assignment.assignment_generation=assigned_unit.fencing_generation AND assignment.membership_generation=assigned_unit.membership_generation AND assignment.budget_generation=assigned_unit.budget_generation AND (j.candidate_id IS NULL OR j.candidate_id=assigned_unit.current_candidate_id) AND j.id=n.job_id '
        'AND j.outcome IS NULL AND (j.fenced=0 OR EXISTS(SELECT 1 FROM job_execution_state capture '
                'WHERE capture.job_id=j.id AND capture.capture_authorized=1 AND capture.result_revoked=0)) '
        'AND NOT EXISTS(SELECT 1 FROM job_execution_state x WHERE x.job_id=j.id AND x.result_revoked=1) '
        "AND (n.reason NOT IN ('cleanup','cleanup_pending') OR j.cleanup=0) AND EXISTS(SELECT 1 FROM job_members s "
        'WHERE s.job_id=j.id AND s.member_id=?))) ORDER BY n.due_at,n.owner,n.scope_key LIMIT 1',
        (unit['id'], unit['version'], unit['fencing_generation'], unit['membership_generation'],
         unit['current_candidate_id'], member_id, member_id)).fetchone()
    return dict(row) if row else None


class Scheduling(Lifecycle):
    def __init__(self, store, *, clock=time.time, retry_seconds=DEFAULT_RETRY, stall_seconds=STALL_SECONDS):
        for value in (retry_seconds, stall_seconds):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError('scheduling bounds must be finite positive seconds')
        self.store, self.clock = store, clock
        self.retry_seconds, self.stall_seconds = retry_seconds, stall_seconds

    def record(self, unit_id, *, owner, reason, due_at=None, job_id=None, candidate_id=None, member_id=None):
        if not isinstance(owner, str) or not owner or not isinstance(reason, str) or not reason or len(reason) > 128:
            raise ValueError('scheduling needs a bounded reason and owner')
        now = self.clock()
        due = now + self.retry_seconds if due_at is None else due_at
        if type(due) not in (int, float) or not math.isfinite(due):
            raise ValueError('schedule due time must be finite')
        scope = 'job:' + job_id if job_id else 'member:' + member_id if member_id else 'unit'
        with self.store.read_snapshot() as check:
            if check.execute("SELECT 1 FROM store_metadata WHERE key='import_only' AND value='true'").fetchone():
                return
        with self.store.write() as db:
            writable(db)
            unit = db.execute('SELECT * FROM units WHERE id=?', (unit_id,)).fetchone()
            if unit is None or unit['state'] in {'merged', 'closed', 'dissolved'}:
                return
            if job_id and not db.execute('SELECT 1 FROM jobs j JOIN attempts assignment ON assignment.id=j.attempt_id JOIN units assigned_unit ON assigned_unit.id=j.unit_id WHERE assignment.assignment_generation=assigned_unit.fencing_generation AND assignment.membership_generation=assigned_unit.membership_generation AND assignment.budget_generation=assigned_unit.budget_generation AND (j.candidate_id IS NULL OR j.candidate_id=assigned_unit.current_candidate_id) AND j.id=? AND j.unit_id=? AND j.outcome IS NULL '
                'AND (j.fenced=0 OR EXISTS(SELECT 1 FROM job_execution_state x WHERE x.job_id=j.id '
                'AND x.capture_authorized=1 AND x.result_revoked=0)) '
                'AND NOT EXISTS(SELECT 1 FROM job_execution_state x WHERE x.job_id=j.id AND x.result_revoked=1)', (job_id, unit_id)).fetchone():
                return
            if member_id and not db.execute('SELECT 1 FROM unit_issues WHERE id=? AND unit_id=? AND left_at IS NULL', (member_id, unit_id)).fetchone():
                raise TransitionError('schedule member must belong to the active unit')
            if candidate_id and candidate_id != unit['current_candidate_id']:
                return
            db.execute('INSERT INTO next_actions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(unit_id,owner,scope_key) DO UPDATE SET '
                'unit_version=excluded.unit_version,fencing_generation=excluded.fencing_generation,'
                'membership_generation=excluded.membership_generation,candidate_id=excluded.candidate_id,'
                'member_id=excluded.member_id,job_id=excluded.job_id,reason=excluded.reason,due_at=excluded.due_at,'
                'created_at=CASE WHEN next_actions.reason=excluded.reason AND next_actions.unit_version=excluded.unit_version '
                'THEN next_actions.created_at ELSE excluded.created_at END,updated_at=excluded.updated_at',
                (unit_id, owner, scope, unit['version'], unit['fencing_generation'], unit['membership_generation'],
                 candidate_id, member_id, job_id, reason, due, now, now))

    def clear(self, unit_id, *, owner, job_id=None):
        with self.store.read_snapshot() as check:
            if check.execute("SELECT 1 FROM store_metadata WHERE key='import_only' AND value='true'").fetchone():
                return
        with self.store.write() as db:
            writable(db)
            sql = 'DELETE FROM next_actions WHERE unit_id=? AND owner=?'
            args = [unit_id, owner]
            if job_id:
                sql += ' AND job_id=?'
                args.append(job_id)
            db.execute(sql, args)

    def pending(self, unit_id, owner, *, job_id=None):
        """A future decision only defers the exact unchanged owning scope."""
        scope='job:'+job_id if job_id else 'unit'
        with self.store.read_snapshot() as db:
            row=db.execute('SELECT n.* FROM next_actions n JOIN units u ON u.id=n.unit_id '
                'WHERE n.unit_id=? AND n.owner=? AND n.scope_key=? AND n.due_at>? '
                'AND n.unit_version=u.version AND n.fencing_generation=u.fencing_generation '
                'AND n.membership_generation=u.membership_generation '
                "AND u.state NOT IN ('merged','closed','dissolved') "
                'AND (n.candidate_id IS NULL OR n.candidate_id=u.current_candidate_id) '
                'AND (n.job_id IS NULL OR EXISTS(SELECT 1 FROM jobs j JOIN attempts assignment ON assignment.id=j.attempt_id JOIN units assigned_unit ON assigned_unit.id=j.unit_id WHERE assignment.assignment_generation=assigned_unit.fencing_generation AND assignment.membership_generation=assigned_unit.membership_generation AND assignment.budget_generation=assigned_unit.budget_generation AND (j.candidate_id IS NULL OR j.candidate_id=assigned_unit.current_candidate_id) AND j.id=n.job_id AND j.unit_id=u.id '
                'AND j.outcome IS NULL AND (j.fenced=0 OR EXISTS(SELECT 1 FROM job_execution_state capture '
                'WHERE capture.job_id=j.id AND capture.capture_authorized=1 AND capture.result_revoked=0)) '
                'AND NOT EXISTS(SELECT 1 FROM job_execution_state x WHERE x.job_id=j.id AND x.result_revoked=1) '
                "AND (n.reason NOT IN ('cleanup','cleanup_pending') OR j.cleanup=0)))", (unit_id,owner,scope,self.clock())).fetchone()
        return dict(row) if row else None

    def outcome(self, unit_id, owner, result, *, job_id=None):
        reason = result.get('pending') or result.get('reason') or result.get('error')
        if reason:
            self.record(unit_id, owner=owner, reason=reason, job_id=job_id)
        else:
            self.clear(unit_id, owner=owner, job_id=job_id)
        return result

    def audit(self, *, after=None, limit=50, maintenance=False):
        """Bounded missing-ownership scan; never charges an issue attempt."""
        now = self.clock()
        limit = _limit(limit)
        orphaned = []
        with self.store.read_snapshot() as check:
            if check.execute("SELECT 1 FROM store_metadata WHERE key='import_only' AND value='true'").fetchone():
                return {'items': [], 'next_cursor': None}
        with self.store.write() as db:
            writable(db)
            rows = db.execute("SELECT m.id,m.unit_id FROM unit_issues m JOIN units u ON u.id=m.unit_id "
                "WHERE m.left_at IS NULL AND u.state NOT IN ('merged','closed','dissolved') AND m.id>? "
                'ORDER BY m.id LIMIT ?', (after or '', limit + 1)).fetchall()
            for member in rows[:limit]:
                unit = dict(db.execute('SELECT * FROM units WHERE id=?', (member['unit_id'],)).fetchone())
                gates = applicable_gates(db, unit, member['id'], unit['current_candidate_id'])
                action = current_in(db, unit, member['id'])
                live = db.execute('SELECT 1 FROM jobs j JOIN job_members s ON s.job_id=j.id '
                    'WHERE j.unit_id=? AND s.member_id=? AND (j.cleanup=0 OR (j.outcome IS NULL AND (j.fenced=0 OR EXISTS(SELECT 1 FROM job_execution_state x WHERE x.job_id=j.id AND x.capture_authorized=1 AND x.result_revoked=0)))) LIMIT 1',
                    (unit['id'], member['id'])).fetchone()
                effect = db.execute("SELECT 1 FROM external_effects e JOIN candidate_effects c ON c.effect_id=e.id "
                    "WHERE e.unit_id=? AND e.status IN ('pending','running','uncertain') "
                    'AND c.candidate_id IS ? LIMIT 1', (unit['id'], unit['current_candidate_id'])).fetchone()
                owned = maintenance or gates or live or effect or action and action['due_at'] + self.stall_seconds > now
                if owned:
                    db.execute('DELETE FROM scheduling_orphans WHERE member_id=?', (member['id'],))
                    continue
                db.execute('INSERT OR IGNORE INTO scheduling_orphans VALUES(?,?,?)', (member['id'], unit['id'], now))
                since = db.execute('SELECT unowned_since FROM scheduling_orphans WHERE member_id=?', (member['id'],)).fetchone()[0]
                if now - since >= self.stall_seconds:
                    orphaned.append({'unit_id': unit['id'], 'member_id': member['id'], 'unowned_since': since})
        return {'items': orphaned, 'next_cursor': rows[limit-1]['id'] if len(rows)>limit else None}

    def escalate(self, unit_id, *, reason, member_id=None, effect_id=None):
        """Open only a locally proven stalled-member or bounded-effect gate."""
        with self.store.read_snapshot() as db:
            unit = dict(db.execute('SELECT * FROM units WHERE id=?',(unit_id,)).fetchone())
            existing=db.execute("SELECT id FROM unit_gates WHERE unit_id=? AND code='forge_problem' AND resolved_at IS NULL "
                "AND json_extract(details_json,'$.source')='scheduling' AND json_extract(details_json,'$.reason')=? "
                "AND (json_extract(details_json,'$.reason')='effect_uncertain' OR member_id IS ?) AND json_extract(details_json,'$.effect_id') IS ?",(unit_id,reason,member_id,effect_id)).fetchone()
            if existing:
                return {'gate_id':existing['id']}
        command = Command(str(uuid.uuid4()), 'supervisor:scheduling',unit_id,unit['version'])
        def change(db, unit):
            nonlocal member_id
            self._live(unit)
            candidate_id = None
            details = {'source':'scheduling','reason':reason,'owner':'forge'}
            if reason == 'stalled':
                orphan = db.execute('SELECT * FROM scheduling_orphans WHERE unit_id=? AND member_id=?',(unit_id,member_id)).fetchone()
                if not db.execute('SELECT 1 FROM unit_issues WHERE id=? AND unit_id=? AND left_at IS NULL',(member_id,unit_id)).fetchone():
                    return {'superseded':True}
                if orphan is None or self.clock()-orphan['unowned_since']<self.stall_seconds:
                    raise TransitionError('stall has not reached its bound')
                action=current_in(db,unit,member_id)
                if applicable_gates(db,unit,member_id,unit['current_candidate_id']) or action and action['due_at']+self.stall_seconds>self.clock():
                    return {'owned':True}
                if db.execute('SELECT 1 FROM jobs j JOIN job_members m ON m.job_id=j.id WHERE j.unit_id=? '
                    'AND m.member_id=? AND (j.cleanup=0 OR (j.outcome IS NULL AND (j.fenced=0 OR EXISTS(SELECT 1 FROM job_execution_state x WHERE x.job_id=j.id AND x.capture_authorized=1 AND x.result_revoked=0))))',(unit_id,member_id)).fetchone():
                    return {'owned':True}
                last=db.execute('SELECT reason FROM next_actions WHERE unit_id=? AND (member_id IS NULL OR member_id=?) ORDER BY updated_at DESC LIMIT 1',(unit_id,member_id)).fetchone()
                details.update(unowned_since=orphan['unowned_since'],last_decline=last['reason'] if last else None,exit='recheck')
            elif reason == 'effect_uncertain':
                effect = db.execute('SELECT e.*,c.candidate_id,COALESCE(l.member_id,t.member_id) AS member_id FROM external_effects e '
                    'LEFT JOIN candidate_effects c ON c.effect_id=e.id LEFT JOIN environment_label_effects l ON l.effect_id=e.id '
                    'LEFT JOIN triage_effect_authority t ON t.effect_id=e.id WHERE e.id=? AND e.unit_id=?',(effect_id,unit_id)).fetchone()
                if (effect is None or effect['status'] not in {'pending','running','uncertain'}
                    or effect['escalation_at'] is None):
                    raise TransitionError('effect has not reached its reconciliation bound')
                candidate_id=effect['candidate_id']
                member_id=effect['member_id'] if candidate_id is None else None
                details.update(effect_id=effect_id,uncertain_since=effect['uncertain_since'],exit='reconcile')
            else:
                raise TransitionError('unsupported scheduling fault')
            subject = 'member:'+member_id if member_id else 'candidate:'+candidate_id if candidate_id else 'unit'
            existing = db.execute("SELECT id FROM unit_gates WHERE unit_id=? AND code='forge_problem' AND subject_key=? AND resolved_at IS NULL",(unit_id,subject)).fetchone()
            if existing:
                return {'gate_id':existing['id']}
            return {'gate_id':self._open_gate(db,command,'forge_problem',details,member_id=member_id,candidate_id=candidate_id)}
        return self._apply(command,'escalate_scheduling',{'reason':reason,'member_id':member_id,'effect_id':effect_id},change)

    def recheck(self, command, gate_id, *, explicit=False):
        """Explicit stalled retry and authoritative effect completion have distinct exits."""
        if not explicit:
            with self.store.read_snapshot() as db:
                gate=db.execute("SELECT * FROM unit_gates WHERE id=? AND unit_id=? AND code='forge_problem'",(gate_id,command.unit_id)).fetchone()
                if gate is None or json.loads(gate['details_json']).get('source')!='scheduling':
                    raise TransitionError('not a scheduling gate')
                details=json.loads(gate['details_json'])
                if gate['resolved_at'] is not None:
                    return {'gate_id':gate_id,'resolved':True}
                if details['reason']=='mergeability_unknown':
                    return {'gate_id':gate_id,'pending':'fresh_mergeability'}
                if details['reason']=='stalled':
                    return {'gate_id':gate_id,'pending':'operator_recheck'}
                effect=db.execute('SELECT status FROM external_effects WHERE id=?',(details['effect_id'],)).fetchone()
                if effect is None or effect['status'] not in {'confirmed','failed','superseded'}:
                    return {'gate_id':gate_id,'pending':'effect_reconciliation'}
        def change(db,unit):
            return self.recheck_in(db, command, unit, gate_id, explicit=explicit)
        result=self._apply(command,'recheck_scheduling',{'gate_id':gate_id,'explicit':explicit},change)
        if result.get('resolved') and explicit:
            self.record(command.unit_id,owner='dispatch',reason='operator_recheck',due_at=self.clock(),member_id=result.get('member_id'))
        return result

    def recheck_in(self, db, command, unit, gate_id, *, explicit=False):
        gate=db.execute("SELECT * FROM unit_gates WHERE id=? AND unit_id=? AND code='forge_problem'",(gate_id,unit['id'])).fetchone()
        if gate is None:
            raise TransitionError('scheduling gate does not belong to this unit')
        details=json.loads(gate['details_json'])
        if details.get('source')!='scheduling':
            raise TransitionError('scheduling cannot release another Forge fault')
        if gate['resolved_at'] is not None:
            return {'resolved':True,'gate_id':gate_id}
        if details['reason']=='mergeability_unknown':
            return {'gate_id':gate_id,'pending':'fresh_mergeability'}
        if details['reason']=='stalled':
            if not explicit:
                return {'pending':'operator_recheck','gate_id':gate_id}
            db.execute('DELETE FROM scheduling_orphans WHERE member_id=?',(gate['member_id'],))
        else:
            effect=db.execute('SELECT status FROM external_effects WHERE id=?',(details['effect_id'],)).fetchone()
            if effect is None or effect['status'] not in {'confirmed','failed','superseded'}:
                return {'pending':'effect_reconciliation','gate_id':gate_id}
        self._resolve(db,command,gate_id,{'verified':True,'observed_at':self.clock(),'source':'scheduling'})
        if explicit and details['reason']=='stalled':
            db.execute('INSERT OR REPLACE INTO next_actions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (unit['id'],'dispatch','member:'+gate['member_id'],unit['version']+1,unit['fencing_generation'],unit['membership_generation'],
                 unit['current_candidate_id'],gate['member_id'],None,'operator_recheck',self.clock(),self.clock(),self.clock()))
        return {'resolved':True,'gate_id':gate_id,'member_id':gate['member_id']}

    def cycle(self, *, after=None, limit=50, maintenance=False):
        result=self.audit(after=after,limit=limit,maintenance=maintenance)
        for row in result['items']:
            row.update(self.escalate(row['unit_id'],reason='stalled',member_id=row['member_id']))
        return result
