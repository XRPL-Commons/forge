"""Typed candidate verification receipts and gate release in one transaction.

Only trusted controller/operator adapters call these commands. Worker reports
may request verification, but cannot supply its proof or configure trusted CI.
"""
import json
import math

from .models import TransitionError
from .verification_requests import gate_details_in
from .queries import _limit
from .transitions import Lifecycle, _id, _json, applicable_gates, manifest_hash


def trusted_checks(checks):
    if not isinstance(checks, list) or not 1 <= len(checks) <= 20:
        raise TransitionError('verification needs one to twenty configured CI checks')
    names = set()
    for check in checks:
        if (not isinstance(check, dict) or set(check) != {'name', 'app_id'}
                or not isinstance(check['name'], str) or not check['name'].strip()
                or len(check['name']) > 200 or check['name'].startswith('forge/')
                or check['name'] in names or type(check['app_id']) is not int or check['app_id'] <= 0):
            raise TransitionError('verification checks need unique names and trusted App IDs')
        names.add(check['name'])
    return {(check['name'], check['app_id']) for check in checks}


def worker_receipts_in(db, candidate_id, *, limit=20):
    """Bounded receipt selection; referenced task requests expand without truncation."""
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError('worker verification history supports one to twenty receipts')
    rows = db.execute(
        "SELECT e.id,e.kind,e.actor_id,e.observed_at,g.created_at,g.resolved_at,g.id AS gate_id,"
        "json_extract(g.details_json,'$.verification_job_id') AS verification_job_id,"
        "substr(json_extract(g.details_json,'$.what'),1,1000) AS what,"
        "substr(json_extract(g.details_json,'$.why'),1,1000) AS why,"
        "substr(json_extract(e.details_json,'$.statement'),1,1000) AS statement,"
        "json_extract(e.details_json,'$.checks') AS checks_json,"
        "json_extract(e.details_json,'$.head_oid') AS head_oid,"
        "json_extract(e.details_json,'$.base_oid') AS base_oid "
        'FROM candidate_evidence e JOIN unit_gates g ON g.id=json_extract(e.details_json,\'$.gate_id\') '
        "AND g.candidate_id=e.candidate_id AND g.resolved_at IS NOT NULL AND g.resolution='evidence' "
        "WHERE e.candidate_id=? AND e.kind IN ('verification_ci','verification_human') AND e.verdict='pass' "
        'ORDER BY e.observed_at DESC,e.id DESC LIMIT ?', (candidate_id, limit)).fetchall()
    receipts, bounded_receipts = [], []
    for row in rows:
        checks = json.loads(row['checks_json'] or '[]')[:20]
        ci = [{key: check[key] for key in ('name', 'app_id')} for check in checks]
        proof = dict(via='human' if row['kind'] == 'verification_human' else 'ci',
                     evidence_id=row['id'], observed_at=row['observed_at'],
                     head=row['head_oid'], base=row['base_oid'])
        if row['kind'] == 'verification_human':
            proof.update(actor_id=row['actor_id'], statement=row['statement'])
        else:
            proof['checks'] = [{key: check[key] for key in ('id', 'name', 'app_id')} for check in checks]
        receipt = dict(candidate=candidate_id, what=row['what'] or 'Requested candidate verification',
                       why=row['why'] or '', checks=[check['name'] for check in checks], ci=ci,
                       created_at=row['created_at'], verified_at=row['resolved_at'], proof=proof)
        if row['verification_job_id'] is not None:
            receipt['verification_job_id'] = row['verification_job_id']
        if len(_json(bounded_receipts + [receipt]).encode()) > 60000:
            break
        bounded_receipts.append(receipt)
        # The history budget bounds receipts, not the immutable requests they prove.
        if row['verification_job_id'] is not None:
            gate = db.execute('SELECT * FROM unit_gates WHERE id=?', (row['gate_id'],)).fetchone()
            details = gate_details_in(db, gate)
            receipt = {**receipt, **{key: details[key] for key in ('what', 'why', 'requests')},
                       'requested_checks': details['checks'],
                       **({'final_verification': details['final_verification']} if 'final_verification' in details else {})}
        receipts.append(receipt)
    return list(reversed(receipts))


class Verification(Lifecycle):
    def _admit(self, db, unit, gate_id, kind, proof):
        self._live(unit)
        gate = db.execute('SELECT * FROM unit_gates WHERE id=? AND unit_id=? AND resolved_at IS NULL',
                          (gate_id, unit['id'])).fetchone()
        if gate is None or gate['code'] != kind or gate['candidate_id'] != unit['current_candidate_id']:
            raise TransitionError('verification must identify the current unresolved candidate gate')
        candidate = db.execute('SELECT * FROM candidates WHERE id=? AND unit_id=?',
                               (gate['candidate_id'], unit['id'])).fetchone()
        if (candidate is None or candidate['membership_generation'] != unit['membership_generation']
                or not isinstance(proof, dict) or proof.get('verified') is not True
                or proof.get('candidate_id') != candidate['id']
                or any(proof.get(key) != candidate[key] for key in
                       ('head_oid', 'base_oid', 'policy_hash', 'requirements_hash', 'input_manifest_hash'))
                or any(proof.get(key) != unit[key] for key in ('membership_generation', 'fencing_generation'))):
            raise TransitionError('verification proof does not match current candidate authority')
        observed = proof.get('observed_at')
        if (type(observed) not in (int, float) or not math.isfinite(observed)
                or not max(gate['created_at'], self.clock() - 60) <= observed <= self.clock()):
            raise TransitionError('verification requires fresh trusted evidence after the request')
        if any(row['code'] not in {'verification_human', 'verification_ci', 'approval_required'}
               for row in applicable_gates(db, unit, candidate_id=candidate['id'])):
            raise TransitionError('a hold or readiness gate prevents verification release')
        from .candidate_scope import scope_in
        if scope_in(db,unit)['requirements_hash']!=candidate['requirements_hash']:
            raise TransitionError('Verification requires the current complete requirements scope')
        if len(_json(proof).encode()) > 60000:
            raise TransitionError('verification proof exceeds its size limit')
        return dict(gate), dict(candidate)

    def _complete(self, db, command, gate, proof):
        evidence_id = _id(command.request_id, 'verification')
        details = dict(gate_id=gate['id'], **proof)
        db.execute('INSERT INTO candidate_evidence(id,unit_id,candidate_id,kind,verdict,actor_id,observed_at,details_json) '
                   "VALUES(?,?,?,?,'pass',?,?,?)", (evidence_id, command.unit_id, gate['candidate_id'], gate['code'],
                                                  command.actor_id, proof['observed_at'], _json(details)))
        self._resolve(db, command, gate['id'], {**details, 'evidence_id': evidence_id})
        return {'gate_id': gate['id'], 'evidence_id': evidence_id, 'candidate_id': gate['candidate_id']}

    def complete_ci(self, command, gate_id, *, proof, allowed_checks):
        payload = dict(gate_id=gate_id, proof=proof, allowed_checks=allowed_checks)
        def change(db, unit):
            gate, _ = self._admit(db, unit, gate_id, 'verification_ci', proof)
            allowed = trusted_checks(allowed_checks)
            expected = trusted_checks(json.loads(gate['details_json']).get('ci'))
            if not expected <= allowed or proof.get('profile_hash') != manifest_hash(allowed_checks):
                raise TransitionError('verification check configuration changed')
            rows = proof.get('checks')
            if proof.get('via') != 'ci' or not isinstance(rows, list) or len(rows) != len(expected):
                raise TransitionError('CI verification needs every exact configured check')
            observed = set()
            for row in rows:
                if (not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0
                        or type(row.get('app_id')) is not int or not isinstance(row.get('name'), str)
                        or (row['name'], row['app_id']) not in expected
                        or (row['name'], row['app_id']) in observed
                        or row.get('head_oid') != proof['head_oid'] or row.get('status') != 'completed'
                        or row.get('conclusion') != 'success'):
                    raise TransitionError('CI verification has missing, duplicate or untrusted evidence')
                observed.add((row['name'], row['app_id']))
            return self._complete(db, command, gate, proof)
        return self._apply(command, 'complete_ci_verification', payload, change)

    def complete_human(self, command, gate_id, *, proof):
        payload = dict(gate_id=gate_id, proof=proof)
        def change(db, unit):
            gate, _ = self._admit(db, unit, gate_id, 'verification_human', proof)
            if (proof.get('via') != 'human' or proof.get('operator_confirmed') is not True
                    or proof.get('actor_id') != command.actor_id
                    or not isinstance(proof.get('statement'), str) or not proof['statement'].strip()
                    or len(proof['statement']) > 4000):
                raise TransitionError('human verification needs an explicit attributed operator confirmation')
            return self._complete(db, command, gate, proof)
        return self._apply(command, 'complete_human_verification', payload, change)


class VerificationQueries:
    def __init__(self, store):
        self.store = store

    def pending(self, *, after='', limit=50):
        limit = _limit(limit)
        if not isinstance(after, str):
            raise ValueError('verification cursor must be a gate ID')
        with self.store.read_snapshot() as db:
            rows = [row[0] for row in db.execute("SELECT g.id FROM unit_gates g JOIN units u ON u.id=g.unit_id "
                "WHERE g.code IN ('verification_ci','verification_human') AND g.resolved_at IS NULL "
                "AND g.candidate_id=u.current_candidate_id AND u.state NOT IN ('merged','closed','dissolved') "
                'AND g.id>? ORDER BY g.id LIMIT ?', (after, limit + 1))]
        return {'items': rows[:limit], 'next_cursor': rows[limit - 1] if len(rows) > limit else None}

    def gate(self, gate_id):
        with self.store.read_snapshot() as db:
            if db.execute("SELECT 1 FROM store_metadata WHERE key='import_only' AND value='true'").fetchone():
                raise TransitionError('staging imports cannot verify live candidates')
            gate = db.execute('SELECT * FROM unit_gates WHERE id=?', (gate_id,)).fetchone()
            if gate is None or gate['code'] not in {'verification_human', 'verification_ci'}:
                raise TransitionError('unknown candidate verification gate')
            unit = dict(db.execute('SELECT * FROM units WHERE id=?', (gate['unit_id'],)).fetchone())
            candidate = dict(db.execute('SELECT * FROM candidates WHERE id=?', (gate['candidate_id'],)).fetchone())
            repository = db.execute('SELECT * FROM repositories WHERE id=?', (unit['repository_id'],)).fetchone()
            from .candidate_scope import scope_in
            scope=scope_in(db,unit)
            from .operator_views import reviewer_request_in
            return dict(gate=dict(gate), unit=unit, unit_state=unit['state'],reviewers_requested=reviewer_request_in(db,candidate['id']),identity=candidate, repo=repository['full_name'], repo_id=repository['id'],
                        **scope,
                        target_branch=unit['base_branch'], details=gate_details_in(db,gate),
                        candidate=dict(key=candidate['id'], pr=candidate['pull_number'], head=candidate['head_oid'],
                                       base=candidate['base_oid'], branch=candidate['branch']))
