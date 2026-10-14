"""Durable candidate effects; all remote requests belong outside these commands.

An intent pins its candidate, membership and fencing generation. A pending
intent may be superseded before its first request. Once a merge might have been
sent, a changed local decision cannot erase that uncertainty.
"""
import json
import time
import uuid
import math

from .runtime import writable
from .models import Command, TransitionError, VersionConflict
from .transitions import Lifecycle, _id, _json, _unit, applicable_gates


def retry_policy(reliability=None):
    config=reliability or {}
    return {'retry_seconds':config.get('retry_seconds',30),'retry_cap':config.get('effect_retry_cap_seconds',900),
            'max_attempts':config.get('effect_max_attempts',8),'uncertainty_seconds':config.get('effect_uncertainty_seconds',900)}


def effect_due(row, now):
    return row['retry_at'] is None or row['retry_at']<=now


def admit_send_in(row, now, *, reliability=None):
    policy=retry_policy(reliability)
    if not effect_due(row,now):
        raise TransitionError('effect retry is not due')
    if (row['escalation_at'] is not None or row['attempts']>=policy['max_attempts']
        or row['uncertain_since'] is not None and now-row['uncertain_since']>=policy['uncertainty_seconds']):
        raise TransitionError('effect requires owned reconciliation before another send')


def defer_effect_in(db, row, now, *, reliability=None):
    """Persist backoff after a failed read/send; never increases physical attempts."""
    writable(db)
    policy=retry_policy(reliability)
    delay=policy['retry_cap'] if row['escalation_at'] is not None else min(policy['retry_cap'],policy['retry_seconds']*2**min(max(0,row['attempts']-1),20))
    db.execute("UPDATE external_effects SET status='uncertain',retry_at=?,uncertain_since=COALESCE(uncertain_since,?),updated_at=? "
        "WHERE id=? AND status IN ('pending','running','uncertain')",(now+delay,now,now,row['id']))


class Effects(Lifecycle):
    def __init__(self, store, *, clock=time.time, retry_seconds=30, retry_cap=900, max_attempts=8, uncertainty_seconds=900):
        super().__init__(store, clock=clock)
        if any(type(n) not in (int, float) or not math.isfinite(n) or n <= 0 for n in (retry_seconds, retry_cap, uncertainty_seconds)) or type(max_attempts) is not int or max_attempts < 1:
            raise ValueError('effect retry policy must use positive bounds')
        self.retry_seconds, self.retry_cap = retry_seconds, retry_cap
        self.max_attempts, self.uncertainty_seconds = max_attempts, uncertainty_seconds

    def delay(self, attempts):
        return min(self.retry_cap, self.retry_seconds * 2 ** min(max(0, attempts - 1), 20))

    def _effect_event(self, db, effect_id):
        row = db.execute('SELECT * FROM external_effects WHERE id=?', (effect_id,)).fetchone()
        db.execute('INSERT INTO events(transaction_id,unit_id,kind,at,actor_id,payload_json) '
                   'VALUES(?,?,\'external_effect_changed\',?,\'supervisor:effects\',?)',
                   (row['request_id'], row['unit_id'], self.clock(), _json(dict(effect_id=effect_id,
                    request_id=row['request_id'], kind=row['kind'], status=row['status'], attempts=row['attempts']))))

    @staticmethod
    def _candidate(db, unit, candidate_id):
        row = db.execute('SELECT * FROM candidates WHERE id=? AND unit_id=?',
                         (candidate_id, unit['id'])).fetchone()
        if row is None or unit['current_candidate_id'] != candidate_id:
            raise TransitionError('effect requires the current candidate')
        if row['membership_generation'] != unit['membership_generation']:
            raise TransitionError('candidate membership is stale')
        return dict(row)

    @staticmethod
    def _authorization(candidate, authorization, now):
        if not isinstance(authorization, dict):
            raise TransitionError('merge requires fresh structured authorization')
        if any(authorization.get(key) is not True for key in
               ('verified', 'merge_enabled', 'approved', 'checks_passed', 'protection_ready')):
            raise TransitionError('merge policy has not authorized this candidate')
        if any(authorization.get(key) != candidate[key] for key in
               ('head_oid', 'base_oid', 'policy_hash', 'requirements_hash')) or authorization.get('candidate_id') != candidate['id']:
            raise TransitionError('merge authorization does not match the candidate')
        observed = authorization.get('observed_at')
        if type(observed) not in (int, float) or not now - 60 <= observed <= now:
            raise TransitionError('merge authorization must be a fresh observation')

    def _merge_ready(self, db, unit, candidate, authorization):
        self._authorization(candidate, authorization, self.clock())
        if unit['state'] != 'merging' or applicable_gates(db, unit, candidate_id=candidate['id']):
            raise TransitionError('unit is not admitted for merging')
        if db.execute('SELECT 1 FROM jobs WHERE unit_id=? AND cleanup=0', (unit['id'],)).fetchone():
            raise TransitionError('merge requires verified worker cleanup')
        for role in ('review', 'audit'):
            latest = db.execute('SELECT verdict FROM candidate_evidence WHERE candidate_id=? AND kind=? '
                                'ORDER BY observed_at DESC,rowid DESC LIMIT 1', (candidate['id'], role)).fetchone()
            if latest is None or latest[0] != 'pass':
                raise TransitionError('merge requires current passing review and audit evidence')

    @staticmethod
    def _verdict_ready(db, unit, candidate, job_id):
        job = db.execute('SELECT j.*,a.assignment_generation,a.membership_generation AS assigned_membership,'
                         'a.budget_generation AS assigned_budget,e.capture_authorized,e.result_revoked '
                         'FROM jobs j JOIN attempts a ON a.id=j.attempt_id '
                         'JOIN job_execution_state e ON e.job_id=j.id WHERE j.id=? AND j.unit_id=?',
                         (job_id, unit['id'])).fetchone()
        if (job is None or job['role'] not in {'review', 'audit'} or job['outcome'] is not None
                or not job['capture_authorized'] or job['result_revoked']
                or job['candidate_id'] != candidate['id']
                or job['assignment_generation'] != unit['fencing_generation']
                or job['assigned_membership'] != unit['membership_generation']
                or job['assigned_budget'] != unit['budget_generation']
                or unit['state'] != {'review': 'reviewing', 'audit': 'auditing'}.get(job['role'])):
            raise TransitionError('verdict does not own a current captured assignment')
        for gate in applicable_gates(db, unit, job['member_id'], candidate['id']):
            if gate['code'] not in {'result_pending', 'cleanup_pending'} or json.loads(gate['details_json']).get('job_id') != job_id:
                raise TransitionError('a gate prevents publishing this verdict')
        return dict(job)

    def _insert(self, db, command, unit, candidate, kind, payload, job_id=None):
        effect_id = _id(command.request_id, 'effect')
        db.execute('INSERT INTO external_effects(id,request_id,unit_id,kind,status,expected_version,'
                   'payload_json,created_at,updated_at) VALUES(?,?,?,?,\'pending\',?,?,?,?)',
                   (effect_id, command.request_id, unit['id'], kind, unit['version'] + 1,
                    _json(payload), self.clock(), self.clock()))
        db.execute('INSERT INTO candidate_effects VALUES(?,?,?,?,?)',
                   (effect_id, candidate['id'], job_id, unit['fencing_generation'], unit['membership_generation']))
        self._effect_event(db, effect_id)
        return {'effect_id': effect_id}

    def queue_verdict(self, command, job_id, *, report):
        if (not isinstance(report, dict) or report.get('outcome') not in {'pass', 'reject'}
                or not isinstance(report.get('summary'), str) or len(_json(report).encode()) > 60000):
            raise TransitionError('verdict effect needs a bounded passing or failing report')
        payload = dict(job_id=job_id, report=report)
        def change(db, unit):
            candidate = self._candidate(db, unit, report.get('candidate'))
            self._verdict_ready(db, unit, candidate, job_id)
            return self._insert(db, command, unit, candidate, 'github_verdict', payload, job_id)
        return self._apply(command, 'queue_verdict', payload, change)

    def queue_check(self, command, candidate_id, *, role, conclusion, summary):
        if (role not in {'review', 'audit', 'controller'}
                or conclusion not in {None, 'success', 'failure', 'cancelled'}
                or not isinstance(summary, str) or len(summary.encode()) > 60000):
            raise TransitionError('check effect has an unsupported role, conclusion or summary')
        payload = dict(candidate_id=candidate_id, role=role, conclusion=conclusion, summary=summary)
        def change(db, unit):
            candidate = self._candidate(db, unit, candidate_id)
            # Technical success is published only with a validated job report.
            if conclusion == 'success':
                raise TransitionError('successful checks require verdict or merge authorization')
            return self._insert(db, command, unit, candidate, 'github_check', payload)
        return self._apply(command, 'queue_check', payload, change)

    def queue_merge(self, command, candidate_id, *, policy_hash, authorization):
        payload = dict(candidate_id=candidate_id, policy_hash=policy_hash, authorization=authorization)
        def change(db, unit):
            candidate = self._candidate(db, unit, candidate_id)
            if policy_hash != candidate['policy_hash']:
                raise TransitionError('merge policy changed')
            self._merge_ready(db, unit, candidate, authorization)
            if db.execute("SELECT 1 FROM external_effects WHERE unit_id=? AND status IN ('pending','running','uncertain')",
                          (unit['id'],)).fetchone():
                raise TransitionError('prior external effects require reconciliation')
            result = self._insert(db, command, unit, candidate, 'github_merge', payload)
            db.execute('INSERT INTO merge_requests(id,unit_id,candidate_id,effect_id,fencing_generation,status,created_at) '
                       'VALUES(?,?,?,?,?,\'pending\',?)', (_id(command.request_id, 'merge'), unit['id'], candidate_id,
                                                         result['effect_id'], unit['fencing_generation'], self.clock()))
            return result
        return self._apply(command, 'queue_merge', payload, change)

    def queue_settlement(self, command, candidate_id, *, summary):
        """Cancel remaining Forge checks, including a superseded candidate head."""
        if not isinstance(summary, str) or len(summary.encode()) > 60000:
            raise TransitionError('check settlement requires a bounded summary')
        payload = dict(candidate_id=candidate_id, summary=summary)
        def change(db, unit):
            candidate = db.execute('SELECT * FROM candidates WHERE id=? AND unit_id=?',
                                   (candidate_id, unit['id'])).fetchone()
            if candidate is None:
                raise TransitionError('check settlement must belong to this unit')
            # Freeze ownership before the remote read. A replacement candidate
            # may reuse this commit with different base/policy/requirements;
            # a later retry must never adopt its newly published checks.
            external_ids = [row[0] for row in db.execute(
                'SELECT id FROM jobs WHERE candidate_id=? AND role IN (\'review\',\'audit\') '
                'UNION SELECT effect_id FROM candidate_effects WHERE candidate_id=? ORDER BY 1 LIMIT 501',
                (candidate_id, candidate_id))]
            check_ids = [dict(row) for row in db.execute(
                'SELECT github_id,app_id FROM check_runs WHERE candidate_id=? ORDER BY github_id LIMIT 501',
                (candidate_id,))]
            effect_payload = {**payload, 'external_ids': external_ids, 'check_ids': check_ids}
            if len(external_ids) > 500 or len(check_ids) > 500 or len(_json(effect_payload).encode()) > 65536:
                raise TransitionError('candidate check settlement exceeds its ownership limit')
            return self._insert(db, command, unit, dict(candidate), 'github_settle_checks', effect_payload)
        return self._apply(command, 'queue_settlement', payload, change)

    def claim(self, effect_id, *, expected_version, authorization=None, merge_request=False):
        """Commit possible delivery before any network mutation, under driver lock."""
        with self.store.write() as db:
            writable(db)
            row = db.execute('SELECT e.*,c.candidate_id,c.job_id,c.fencing_generation,c.membership_generation '
                             'FROM external_effects e JOIN candidate_effects c ON c.effect_id=e.id WHERE e.id=?',
                             (effect_id,)).fetchone()
            if row is None or row['status'] not in {'pending', 'running', 'uncertain'}:
                raise TransitionError('effect is not runnable')
            admit_send_in(row,self.clock(),reliability={'effect_max_attempts':self.max_attempts,'effect_uncertainty_seconds':self.uncertainty_seconds})
            if db.execute("SELECT 1 FROM store_metadata WHERE key='import_only' AND value='true'").fetchone():
                raise TransitionError('staging imports cannot issue effects')
            unit = _unit(db, row['unit_id'])
            settlement = row['kind'] == 'github_settle_checks'
            candidate = (dict(db.execute('SELECT * FROM candidates WHERE id=?', (row['candidate_id'],)).fetchone())
                         if settlement else self._candidate(db, unit, row['candidate_id']))
            if not settlement and (row['fencing_generation'] != unit['fencing_generation'] or row['membership_generation'] != unit['membership_generation']):
                raise TransitionError('effect authority has been superseded')
            if type(expected_version) is not int or unit['version'] != expected_version:
                raise VersionConflict('unit changed during effect revalidation')
            if row['kind'] == 'github_merge':
                self._merge_ready(db, unit, candidate, authorization)
            elif row['kind'] == 'github_verdict':
                self._verdict_ready(db, unit, candidate, row['job_id'])
            # The preliminary merge check is not another merge PUT attempt.
            increment = int(row['kind'] != 'github_merge' or merge_request)
            db.execute("UPDATE external_effects SET status='running',attempts=attempts+?,updated_at=?,"
                       'retry_at=CASE WHEN ? THEN ? ELSE retry_at END,'
                       'uncertain_since=CASE WHEN ? THEN COALESCE(uncertain_since,?) ELSE uncertain_since END WHERE id=?',
                       (increment,self.clock(),increment,self.clock()+self.delay(row['attempts']+increment),increment,self.clock(),effect_id))
            # Check publication is recoverable independently. Only a possible
            # merge PUT must prevent later membership changes indefinitely.
            if merge_request:
                if row['kind'] != 'github_merge':
                    raise TransitionError('only a merge effect can send a merge request')
                db.execute("UPDATE merge_requests SET status='uncertain',send_count=send_count+1,last_sent_at=? WHERE effect_id=?",
                           (self.clock(), effect_id))
            self._effect_event(db, effect_id)

    def record(self, effect_id, status, *, receipt=None):
        if status not in {'uncertain', 'confirmed', 'failed', 'superseded'}:
            raise TransitionError('unsupported effect result')
        if receipt is not None and (not isinstance(receipt, dict) or len(_json(receipt).encode()) > 65536):
            raise TransitionError('effect receipt must be a bounded object')
        with self.store.write() as db:
            writable(db)
            row = db.execute('SELECT * FROM external_effects WHERE id=?', (effect_id,)).fetchone()
            if row is None:
                raise KeyError(effect_id)
            if row['status'] in {'confirmed', 'failed', 'superseded'}:
                return
            merge = db.execute('SELECT status FROM merge_requests WHERE effect_id=?', (effect_id,)).fetchone()
            if row['kind'] == 'github_merge' and (status == 'confirmed' or merge and merge[0] == 'uncertain' and status != 'uncertain'):
                raise TransitionError('a possibly sent merge requires authoritative lifecycle reconciliation')
            uncertain = status == 'uncertain'
            db.execute('UPDATE external_effects SET status=?,receipt_json=?,updated_at=?,retry_at=?,'
                       'uncertain_since=CASE WHEN ? THEN COALESCE(uncertain_since,?) ELSE uncertain_since END WHERE id=?',
                       (status, _json(receipt) if receipt is not None else None, self.clock(),
                        self.clock() + (self.retry_cap if row['escalation_at'] is not None else self.delay(row['attempts'])) if uncertain else None,
                        uncertain, self.clock(), effect_id))
            if row['kind'] == 'github_merge' and status == 'superseded':
                db.execute("UPDATE merge_requests SET status='superseded' WHERE effect_id=?", (effect_id,))
            self._effect_event(db, effect_id)

    def refuse_merge(self, effect_id, *, http_status, evidence):
        """Record a definitive refusal of this PUT; transport/5xx remain uncertain."""
        if type(http_status) is not int or http_status not in {400, 401, 403, 404, 405, 409, 422}:
            raise TransitionError('merge response does not prove refusal')
        with self.store.write() as db:
            writable(db)
            row = db.execute('SELECT e.status,m.send_count,m.last_sent_at,m.candidate_id,c.pull_number,c.head_oid,u.repository_id '
                             'FROM external_effects e JOIN merge_requests m ON m.effect_id=e.id '
                             'JOIN candidates c ON c.id=m.candidate_id JOIN units u ON u.id=e.unit_id '
                             "WHERE e.id=? AND e.kind='github_merge' AND m.status='uncertain'", (effect_id,)).fetchone()
            if row is None or row['status'] not in {'running', 'uncertain'}:
                raise TransitionError('refusal must identify a possibly sent merge request')
            # A later refusal cannot account for an earlier ambiguous PUT.
            # Preserve that uncertainty even when another request is refused.
            if row['send_count'] != 1:
                raise TransitionError('an earlier merge request still requires reconciliation')
            if (not isinstance(evidence, dict) or evidence.get('verified') is not True or evidence.get('merged') is not False
                    or any(evidence.get(key) != row[key] for key in ('candidate_id', 'pull_number', 'head_oid', 'repository_id'))
                    or not isinstance(evidence.get('observed_head'), str) or not evidence['observed_head']
                    or type(evidence.get('observed_at')) not in (int, float)
                    or not row['last_sent_at'] <= evidence['observed_at'] <= self.clock()):
                raise TransitionError('refused merge needs a fresh authoritative unmerged PR observation')
            db.execute("UPDATE external_effects SET status='failed',receipt_json=?,updated_at=? WHERE id=?",
                       (_json({'reason': 'merge_refused', 'http_status': http_status, 'evidence': evidence}), self.clock(), effect_id))
            db.execute("UPDATE merge_requests SET status='refused' WHERE effect_id=?", (effect_id,))
            self._effect_event(db, effect_id)

    def bounded(self, effect_id):
        """Pin escalation once, retain the effect for authoritative reads only."""
        with self.store.write() as db:
            writable(db)
            row=db.execute('SELECT * FROM external_effects WHERE id=?',(effect_id,)).fetchone()
            if row is None or row['status'] not in {'pending','running','uncertain'}:
                return False
            exhausted = row['attempts'] >= self.max_attempts
            elapsed = row['uncertain_since'] is not None and self.clock()-row['uncertain_since'] >= self.uncertainty_seconds
            if not exhausted and not elapsed and row['escalation_at'] is None:
                return False
            db.execute('UPDATE external_effects SET escalation_at=COALESCE(escalation_at,?),retry_at=? WHERE id=?',
                (self.clock(),self.clock()+self.retry_cap,effect_id))
        from .scheduling import Scheduling
        Scheduling(self.store,clock=self.clock).escalate(row['unit_id'],reason='effect_uncertain',effect_id=effect_id)
        return True

    def observe_unmerged(self, effect_id, *, evidence):
        """Fresh remote state reconciles every earlier SHA-bound PUT, not a refusal."""
        with self.store.read_snapshot() as db:
            scope = db.execute('SELECT e.unit_id,u.version FROM external_effects e JOIN units u ON u.id=e.unit_id WHERE e.id=?',(effect_id,)).fetchone()
        if scope is None:
            raise KeyError(effect_id)
        command=Command(str(uuid.uuid4()),'supervisor:effects',scope['unit_id'],scope['version'])
        def change(db,unit):
            row = db.execute('SELECT e.*,m.send_count,m.last_sent_at,m.candidate_id,c.pull_number,c.head_oid,u.repository_id,u.base_branch '
                'FROM external_effects e JOIN merge_requests m ON m.effect_id=e.id '
                'JOIN candidates c ON c.id=m.candidate_id JOIN units u ON u.id=e.unit_id '
                "WHERE e.id=? AND m.status='uncertain'", (effect_id,)).fetchone()
            if row is None:
                raise TransitionError('observation must reconcile a possibly sent merge')
            if (not isinstance(evidence, dict) or evidence.get('verified') is not True or evidence.get('merged') is not False
                or any(evidence.get(key) != row[key] for key in ('candidate_id','pull_number','head_oid','repository_id','base_branch'))
                or type(evidence.get('observed_at')) not in (int,float)
                or row['last_sent_at'] is None or not row['last_sent_at'] <= evidence['observed_at'] <= self.clock()
                or not isinstance(evidence.get('observed_head'), str) or not evidence['observed_head']
                or not (evidence.get('state') == 'closed' or evidence['observed_head'] != row['head_oid'])):
                raise TransitionError('fresh exact repository PR evidence must establish closure or changed head')
            db.execute("UPDATE external_effects SET status='failed',receipt_json=?,retry_at=NULL,updated_at=? WHERE id=?",
                (_json({'reason':'merge_observed_unmerged','evidence':evidence}),self.clock(),effect_id))
            db.execute("UPDATE merge_requests SET status='refused' WHERE effect_id=?",(effect_id,))
            self._effect_event(db,effect_id)
            if unit['current_candidate_id']==row['candidate_id']:
                self._invalidate(db,command,unit,'candidate_changed',evidence)
            return {'effect_id':effect_id,'reconciled':True}
        return self._apply(command,'observe_unmerged_effect',{'effect_id':effect_id,'evidence':evidence},change)

    def resume_unsent_merge(self, effect_id, *, evidence):
        """A fresh mergeable PR can release an observation-only wait, never a sent PUT."""
        with self.store.read_snapshot() as db:
            row=db.execute('SELECT e.unit_id,u.version,e.escalation_at FROM external_effects e JOIN units u ON u.id=e.unit_id WHERE e.id=?',(effect_id,)).fetchone()
        if row is None or row['escalation_at'] is None:
            return False
        command=Command(str(uuid.uuid4()),'supervisor:effects',row['unit_id'],row['version'])
        def change(db,unit):
            row=db.execute('SELECT e.*,m.send_count,m.candidate_id,c.pull_number,c.head_oid,c.base_oid FROM external_effects e '
                "JOIN merge_requests m ON m.effect_id=e.id JOIN candidates c ON c.id=m.candidate_id WHERE e.id=? AND m.status='pending'",(effect_id,)).fetchone()
            if (row is None or row['send_count']!=0 or row['candidate_id']!=unit['current_candidate_id']
                or not isinstance(evidence,dict) or evidence.get('verified') is not True or evidence.get('mergeable') is not True
                or evidence.get('merged') is not False or evidence.get('state')!='open'
                or any(evidence.get(key)!=row[key] for key in ('candidate_id','pull_number','head_oid','base_oid'))
                or evidence.get('repository_id')!=unit['repository_id'] or evidence.get('base_branch')!=unit['base_branch']
                or type(evidence.get('observed_at')) not in (int,float) or not self.clock()-60<=evidence['observed_at']<=self.clock()):
                raise TransitionError('only fresh exact mergeability may resume an unsent intent')
            for gate in db.execute("SELECT id FROM unit_gates WHERE unit_id=? AND code='forge_problem' AND resolved_at IS NULL "
                "AND json_extract(details_json,'$.source')='scheduling' AND json_extract(details_json,'$.effect_id')=?",(unit['id'],effect_id)).fetchall():
                self._resolve(db,command,gate['id'],evidence)
            db.execute('UPDATE external_effects SET escalation_at=NULL,uncertain_since=NULL,conflict_since=NULL,retry_at=NULL,updated_at=? WHERE id=?',
                (self.clock(),effect_id))
            self._effect_event(db,effect_id)
            return {'effect_id':effect_id,'resumed':True}
        return self._apply(command,'resume_unsent_merge',{'effect_id':effect_id,'evidence':evidence},change)

    def rebuild_conflict(self, effect_id):
        """Atomically withdraw an unsent intent and rebuild a sustained conflict."""
        with self.store.read_snapshot() as db:
            scope=db.execute('SELECT e.unit_id,u.version FROM external_effects e JOIN units u ON u.id=e.unit_id WHERE e.id=?',(effect_id,)).fetchone()
        command=Command(str(uuid.uuid4()),'supervisor:effects',scope['unit_id'],scope['version'])
        def change(db,unit):
            row=db.execute('SELECT e.*,m.candidate_id,m.send_count FROM external_effects e JOIN merge_requests m ON m.effect_id=e.id '
                "WHERE e.id=? AND m.status='pending'",(effect_id,)).fetchone()
            if (row is None or row['send_count']!=0 or row['conflict_since'] is None
                or self.clock()-row['conflict_since']<self.uncertainty_seconds
                or self.clock()-row['updated_at']>60 or row['candidate_id']!=unit['current_candidate_id']):
                raise TransitionError('rebuild requires a fresh sustained conflict without uncertain sends')
            evidence={'verified':True,'observed_at':self.clock(),'conflict_since':row['conflict_since'],'effect_id':effect_id}
            db.execute("UPDATE external_effects SET status='superseded',receipt_json=?,retry_at=NULL,updated_at=? WHERE id=?",
                (_json({'reason':'observed_merge_conflict','evidence':evidence}),self.clock(),effect_id))
            db.execute("UPDATE merge_requests SET status='superseded' WHERE effect_id=?",(effect_id,))
            self._effect_event(db,effect_id)
            self._invalidate(db,command,unit,'merge_conflict',evidence)
            return {'effect_id':effect_id,'rebuilding':True}
        return self._apply(command,'rebuild_observed_conflict',{'effect_id':effect_id},change)

    def observe_wait(self, effect_id, *, conflict=False):
        with self.store.write() as db:
            writable(db)
            row = db.execute('SELECT * FROM external_effects WHERE id=?',(effect_id,)).fetchone()
            if row is None or row['status'] not in {'pending','running','uncertain'}:
                return
            db.execute('UPDATE external_effects SET uncertain_since=COALESCE(uncertain_since,?),'
                'conflict_since=CASE WHEN ? THEN COALESCE(conflict_since,?) ELSE NULL END,retry_at=?,updated_at=? WHERE id=?',
                (self.clock(),conflict,self.clock(),self.clock()+self.delay(row['attempts']),self.clock(),effect_id))



class EffectQueries:
    def __init__(self, store, *, clock=time.time):
        self.store, self.clock = store, clock

    def pending(self, *, limit=20, after=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('effect page must contain between 1 and 100 rows')
        with self.store.read_snapshot() as db:
            cursor = '' if after is None else 'AND (e.created_at,e.id) > (?,?) '
            parameters = (self.clock(),limit) if after is None else (self.clock(),*after,limit)
            return [row[0] for row in db.execute(
                "SELECT e.id FROM external_effects e JOIN candidate_effects c ON c.effect_id=e.id "
                "WHERE e.status IN ('pending','running','uncertain') AND (e.retry_at IS NULL OR e.retry_at<=?) " + cursor +
                'ORDER BY e.created_at,e.id LIMIT ?', parameters)]

    def get(self, effect_id):
        with self.store.read_snapshot() as db:
            row = db.execute('SELECT e.*,a.candidate_id,a.job_id,a.fencing_generation,a.membership_generation,'
                             'u.version AS unit_version,u.base_branch AS target_branch,r.id AS repo_id,r.full_name AS repo '
                             'FROM external_effects e JOIN candidate_effects a ON a.effect_id=e.id '
                             'JOIN units u ON u.id=e.unit_id JOIN repositories r ON r.id=u.repository_id WHERE e.id=?',
                             (effect_id,)).fetchone()
            if row is None:
                raise KeyError(effect_id)
            value = dict(row)
            value['payload'] = json.loads(value.pop('payload_json'))
            value['receipt'] = json.loads(value.pop('receipt_json') or 'null')
            candidate = dict(db.execute('SELECT * FROM candidates WHERE id=?', (value['candidate_id'],)).fetchone())
            value['identity'] = candidate
            merge = db.execute('SELECT status,send_count,last_sent_at FROM merge_requests WHERE effect_id=?', (effect_id,)).fetchone()
            value['merge_status'] = merge['status'] if merge else None
            value['merge_send_count'] = merge['send_count'] if merge else 0
            value['merge_last_sent_at'] = merge['last_sent_at'] if merge else None
            value['candidate'] = dict(key=candidate['id'], pr=candidate['pull_number'], branch=candidate['branch'],
                                      head=candidate['head_oid'], base=candidate['base_oid'],
                                      requirements=candidate['requirements_hash'], accepted_at=candidate['accepted_at'])
            value['evidence'] = {}
            for kind in ('review', 'audit'):
                evidence = db.execute('SELECT details_json FROM candidate_evidence WHERE candidate_id=? AND kind=? '
                                      'ORDER BY observed_at DESC,rowid DESC LIMIT 1', (candidate['id'], kind)).fetchone()
                if evidence:
                    value['evidence'][kind] = json.loads(evidence[0])
            return value

    def by_request(self, request_id):
        with self.store.read_snapshot() as db:
            row = db.execute('SELECT id FROM external_effects WHERE request_id=?', (request_id,)).fetchone()
        return self.get(row[0]) if row else None

    def verdict(self, job_id):
        with self.store.read_snapshot() as db:
            row = db.execute('SELECT effect_id FROM candidate_effects WHERE job_id=?', (job_id,)).fetchone()
        return self.get(row[0]) if row else None
