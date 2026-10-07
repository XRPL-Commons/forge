"""Durable candidate effects; all remote requests belong outside these commands.

An intent pins its candidate, membership and fencing generation. A pending
intent may be superseded before its first request. Once a merge might have been
sent, a changed local decision cannot erase that uncertainty.
"""
import json

from .models import TransitionError, VersionConflict
from .transitions import Lifecycle, _id, _json, _unit, applicable_gates


class Effects(Lifecycle):
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
            row = db.execute('SELECT e.*,c.candidate_id,c.job_id,c.fencing_generation,c.membership_generation '
                             'FROM external_effects e JOIN candidate_effects c ON c.effect_id=e.id WHERE e.id=?',
                             (effect_id,)).fetchone()
            if row is None or row['status'] not in {'pending', 'running', 'uncertain'}:
                raise TransitionError('effect is not runnable')
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
            db.execute("UPDATE external_effects SET status='running',attempts=attempts+1,updated_at=? WHERE id=?",
                       (self.clock(), effect_id))
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
            row = db.execute('SELECT * FROM external_effects WHERE id=?', (effect_id,)).fetchone()
            if row is None:
                raise KeyError(effect_id)
            if row['status'] in {'confirmed', 'failed', 'superseded'}:
                return
            merge = db.execute('SELECT status FROM merge_requests WHERE effect_id=?', (effect_id,)).fetchone()
            if row['kind'] == 'github_merge' and (status == 'confirmed' or merge and merge[0] == 'uncertain' and status != 'uncertain'):
                raise TransitionError('a possibly sent merge requires authoritative lifecycle reconciliation')
            db.execute('UPDATE external_effects SET status=?,receipt_json=?,updated_at=? WHERE id=?',
                       (status, _json(receipt) if receipt is not None else None, self.clock(), effect_id))
            if row['kind'] == 'github_merge' and status == 'superseded':
                db.execute("UPDATE merge_requests SET status='superseded' WHERE effect_id=?", (effect_id,))
            self._effect_event(db, effect_id)

    def refuse_merge(self, effect_id, *, http_status, evidence):
        """Record a definitive refusal of this PUT; transport/5xx remain uncertain."""
        if type(http_status) is not int or http_status not in {400, 401, 403, 404, 405, 409, 422}:
            raise TransitionError('merge response does not prove refusal')
        with self.store.write() as db:
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


class EffectQueries:
    def __init__(self, store):
        self.store = store

    def pending(self, *, limit=20, after=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('effect page must contain between 1 and 100 rows')
        with self.store.read_snapshot() as db:
            cursor = '' if after is None else 'AND (e.created_at,e.id) > (?,?) '
            parameters = (limit,) if after is None else (*after, limit)
            return [row[0] for row in db.execute(
                "SELECT e.id FROM external_effects e JOIN candidate_effects c ON c.effect_id=e.id "
                "WHERE e.status IN ('pending','running','uncertain') " + cursor +
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
            merge = db.execute('SELECT status,send_count FROM merge_requests WHERE effect_id=?', (effect_id,)).fetchone()
            value['merge_status'] = merge['status'] if merge else None
            value['merge_send_count'] = merge['send_count'] if merge else 0
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
