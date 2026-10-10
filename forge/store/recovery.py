"""One investigation per failed assignment; findings never authorize publication.

Recovery leases bypass their own recovery wait and the failure budget. Operator,
ownership and setup gates still block the worker. Closure, changed requirements
and invalidated current outputs stop implementation but cannot erase the
diagnostic obligation against preserved inputs.
Publication recovery is a separate controller-verified command, bound to the
original build assignment, which corrects its original charge exactly once.
"""
from dataclasses import asdict
import json
import re

from .artifacts import Artifacts, ArtifactExpired
from .executions import Executions, ExecutionQueries, Launch, insert_launch, _artifact
from .models import Candidate, TransitionError
from .queries import _limit
from .transitions import _charges, _id, _json, applicable_gates, manifest_hash


def record_failure_facts(db, unit, job, evidence, now, *, store, retry_stage=None):
    """Called inside the failure transaction, after the durable result is saved."""
    report = evidence.get('report') or {}
    cause = evidence.get('reason') or (report.get('summary') if isinstance(report, dict) else None) or 'unverified_execution'
    position = db.execute('SELECT COALESCE(MAX(position),-1)+1 FROM attempt_failures WHERE unit_id=?', (unit['id'],)).fetchone()[0]
    db.execute('INSERT INTO attempt_failures(attempt_id,unit_id,position,role,cause,investigation,retry_stage,evidence_kind,at,recovery_required) '
               "VALUES(?,?,?,?,?,'pending',?,'failure_evidence',?,1)",
               (job['attempt_id'], unit['id'], position, job['role'], str(cause)[:2000], retry_stage or job['role'], now))
    try:
        excerpt = Artifacts(store).read(job['id'], 'logs').content.decode('utf-8', errors='replace')[-16384:]
    except (KeyError, ArtifactExpired):
        excerpt = '[Preserved log evidence unavailable]'
    _artifact(db, job['id'], 'failure_evidence', _json({**evidence, 'logs_excerpt': excerpt}).encode(), now)


def recovery_manifest_in(db, failed):
    manifest = dict(failed_job_id=failed['id'], failed_attempt_id=failed['attempt_id'],
                    failed_input_manifest_hash=manifest_hash(json.loads(failed['input_manifest_json'])))
    members = [dict(row) for row in db.execute('SELECT member_id,requirements_revision_id FROM job_members WHERE job_id=? ORDER BY member_id', (failed['id'],))]
    if failed['member_id'] is not None:
        manifest['requirements_revision_id'] = next(member['requirements_revision_id'] for member in members if member['member_id']==failed['member_id'])
    else:
        manifest['members'] = members
    return manifest


def pending_mandatory(db, unit_id):
    return db.execute("SELECT 1 FROM attempt_failures WHERE unit_id=? AND recovery_required=1 AND investigation!='complete' LIMIT 1", (unit_id,)).fetchone() is not None


def recovery_gates(db, unit, failed_job_id, member_id, candidate_id=None, recovery_job_id=None, *, diagnostic=True):
    pins = db.execute('SELECT member_id FROM job_members WHERE job_id=? ORDER BY member_id', (failed_job_id,)).fetchall()
    if not pins:
        raise TransitionError('recovery requires the complete original assignment scope')
    gates, seen = [], set()
    for pin in pins:
        for gate in applicable_gates(db, unit, pin['member_id'], candidate_id, job_id=recovery_job_id or failed_job_id):
            identity = (gate['id'], gate['code'], gate.get('member_id'), gate.get('job_id'), gate['details_json'])
            if identity not in seen:
                seen.add(identity); gates.append(gate)
    blocked = []
    for gate in gates:
        details = json.loads(gate['details_json'])
        if diagnostic and (
                gate['code'] == 'external_change' and gate['id'] is None and details.get('reason') == 'issue_closed'
                or gate['code'] == 'membership_decision' and details.get('reason') in {
                    'member_closed', 'requirements_changed', 'requirements_revision_changed'}
                or gate['code'] == 'dependency' and gate['id'] is None
                   and details.get('reason') == 'prerequisite_output_pending'):
            # These gates retain the approved implementation scope. A recovery
            # reads its original failure and cannot change that scope.
            continue
        if (diagnostic and gate['code'] == 'repository_setup'
                and details.get('source') == 'repository_readiness'
                and details.get('access_allowed') is True
                and details.get('repository_id') == unit['repository_id']
                and details.get('base_branch') == unit['base_branch']
                and isinstance(details.get('source_revision'), str) and details['source_revision']):
            # Explicit current access still admits diagnostics; implementation
            # and publication correction continue to require the setup gate.
            continue
        if gate['code'] == 'budget_exhausted':
            continue
        if gate['code'] == 'recovery_pending' and details.get('job_id') == failed_job_id:
            continue
        if recovery_job_id and gate['code'] in {'cleanup_pending', 'result_pending'} and details.get('job_id') == recovery_job_id:
            continue
        blocked.append(gate)
    return blocked


def execution_gates(db, unit, job):
    """The runner and reservation share the exact recovery gate exception."""
    if job['role'] != 'recovery':
        from .task_inputs import task_execution_gates_in
        return task_execution_gates_in(db, unit, job)
    assignment = db.execute('SELECT failed_job_id FROM recovery_assignments WHERE job_id=?', (job['id'],)).fetchone()
    if assignment is None:
        # An imported recovery without the typed authority cannot run.
        return [{'code': 'legacy_reconciliation'}]
    return recovery_gates(db, unit, assignment[0], job['member_id'], job['candidate_id'], job['id'])


def _findings(value, incomplete):
    if type(incomplete) is not bool or not isinstance(value, dict) or len(_json(value).encode()) > 60000:
        raise TransitionError('recovery requires bounded structured findings')
    if (not all(isinstance(value.get(key), str) for key in ('summary', 'pattern', 'uncertainty'))
            or not isinstance(value.get('evidence'), list)
            or any(not isinstance(item, str) for item in value['evidence'])):
        raise TransitionError('findings require summary, pattern, evidence and uncertainty')
    alternative = value.get('alternative')
    if alternative is not None and (not isinstance(alternative, dict)
            or set(alternative) != {'change', 'reason', 'evidence'}
            or any(not isinstance(item, str) or not item.strip() for item in alternative.values())):
        raise TransitionError('alternative requires change, reason and supporting evidence')
    return {**value, 'incomplete': incomplete}


def _verification_gates(gates):
    if (not isinstance(gates, (list, tuple)) or any(not isinstance(gate, dict)
            or set(gate) != {'code', 'details'} or gate['code'] not in {'verification_human', 'verification_ci'}
            or not isinstance(gate['details'], dict) or not gate['details'] for gate in gates)
            or len({gate['code'] for gate in gates}) != len(gates)):
        raise TransitionError('recovery verification gates require exact candidate-scoped details')
    return list(gates)


def failure_context(store, db, recovery_job_id):
    row = db.execute('SELECT j.id,j.attempt_id,j.role,j.candidate_id,a.input_manifest_json,f.cause,f.retry_stage '
        'FROM recovery_assignments r JOIN jobs j ON j.id=r.failed_job_id JOIN attempts a ON a.id=j.attempt_id '
        'JOIN attempt_failures f ON f.attempt_id=j.attempt_id WHERE r.job_id=?', (recovery_job_id,)).fetchone()
    if row is None:
        raise TransitionError('recovery has no exact failed assignment')
    value = dict(attempt=row['id'], attempt_id=row['attempt_id'], role=row['role'], cause=row['cause'],
                 retry_stage=row['retry_stage'], candidate_id=row['candidate_id'],
                 input_manifest=json.loads(row['input_manifest_json']))
    artifacts = Artifacts(store)
    for kind in ('failure_evidence', 'worker_capture'):
        try:
            value[kind] = json.loads(artifacts.read(row['id'], kind).content)
        except (KeyError, ArtifactExpired):
            if kind=='worker_capture' and db.execute('SELECT 1 FROM migration_recovery_adoptions WHERE failed_job_id=?',(row['id'],)).fetchone():
                try:value[kind]=json.loads(artifacts.read(row['id'],'result').content)
                except (KeyError,ArtifactExpired):value[kind]={'unavailable':True}
            else:value[kind] = {'unavailable': True}
    if db.execute('SELECT 1 FROM migration_recovery_adoptions WHERE failed_job_id=?',(row['id'],)).fetchone():
        for kind in ('failure_evidence','worker_capture'):
            diagnostic=_json(value[kind])
            if len(diagnostic.encode())>16384:
                value[kind]={'truncated':True,'digest':manifest_hash(value[kind]),'excerpt':diagnostic[:4000],
                             'artifact':{'job_id':row['id'],'kind':kind}}
    try:
        value['logs'] = artifacts.read(row['id'], 'logs').content.decode('utf-8', errors='replace')[-16384:]
    except (KeyError, ArtifactExpired):
        value['logs'] = (value['failure_evidence'].get('logs_excerpt') if isinstance(value['failure_evidence'],dict) else None) or '[Preserved log evidence unavailable]'
    return value


class Recovery(Executions):
    def reserve(self, command, failed_job_id, *, slot, execution_fence, launch):
        if not isinstance(launch, Launch) or not slot or not execution_fence:
            raise TransitionError('recovery reservation requires lease and Launch inputs')
        payload = dict(failed_job_id=failed_job_id, slot=slot, execution_fence=execution_fence, launch=launch.payload())
        def change(db, unit):
            self._live(unit)
            failed = db.execute('SELECT j.*,a.input_manifest_json FROM jobs j JOIN attempts a ON a.id=j.attempt_id '
                                'WHERE j.id=? AND j.unit_id=?', (failed_job_id, unit['id'])).fetchone()
            failure = db.execute('SELECT * FROM attempt_failures WHERE attempt_id=?', (failed['attempt_id'],)).fetchone() if failed else None
            if (failed is None or failed['role'] == 'recovery' or not failed['cleanup'] or (failed['outcome'] not in {'failure', 'rejected'} and not (failed['outcome']=='failed' and db.execute("SELECT 1 FROM migration_recovery_adoptions WHERE failed_job_id=? AND disposition='investigate'",(failed['id'],)).fetchone()))
                    or failure is None or not failure['recovery_required'] or failure['investigation'] != 'pending'):
                raise TransitionError('recovery requires an uninvestigated cleaned confirmed failure')
            if db.execute('SELECT 1 FROM recovery_assignments WHERE failed_job_id=?', (failed_job_id,)).fetchone():
                raise TransitionError('failed assignment already has its investigation')
            # The failed physical execution is already cleaned above. Its
            # diagnostic reader does not wait for unrelated task processes,
            # including independent contributions to the same issue.
            gates = db.execute("SELECT details_json FROM unit_gates WHERE unit_id=? AND code='recovery_pending' AND resolved_at IS NULL", (unit['id'],)).fetchall()
            if not any(json.loads(row[0]).get('job_id') == failed_job_id for row in gates):
                raise TransitionError('failed assignment no longer awaits recovery')
            if recovery_gates(db, unit, failed_job_id, failed['member_id'], unit['current_candidate_id']):
                raise TransitionError('an applicable gate blocks recovery')
            attempt_id, job_id = _id(command.request_id, 'attempt'), _id(command.request_id, 'job')
            manifest = recovery_manifest_in(db, failed)
            db.execute('INSERT INTO attempts(id,unit_id,member_id,membership_generation,assignment_generation,input_manifest_json,budget_generation,created_at) '
                'VALUES(?,?,?,?,?,?,?,?)', (attempt_id, unit['id'], failed['member_id'], unit['membership_generation'], unit['fencing_generation'],
                                           _json(manifest), unit['budget_generation'], self.clock()))
            db.execute("INSERT INTO jobs(id,unit_id,attempt_id,member_id,candidate_id,role,phase,created_at) VALUES(?,?,?,?,?,'recovery','reserved',?)",
                       (job_id, unit['id'], attempt_id, failed['member_id'], unit['current_candidate_id'], self.clock()))
            pins = db.execute('SELECT member_id,requirements_revision_id FROM job_members WHERE job_id=?', (failed_job_id,)).fetchall()
            if not pins:
                raise TransitionError('recovery requires the complete original assignment scope')
            db.executemany('INSERT INTO job_members(job_id,member_id,requirements_revision_id) VALUES(?,?,?)',
                           [(job_id, pin['member_id'], pin['requirements_revision_id']) for pin in pins])
            db.execute('INSERT INTO job_leases(slot,job_id,execution_fence,acquired_at) VALUES(?,?,?,?)', (slot, job_id, execution_fence, self.clock()))
            db.execute('INSERT INTO recovery_assignments VALUES(?,?,?,?)', (job_id, failed_job_id, failed['attempt_id'], self.clock()))
            insert_launch(db, job_id, slot, execution_fence, payload['launch'], self.clock())
            db.execute('UPDATE attempt_failures SET investigation=? WHERE attempt_id=?', (job_id, failed['attempt_id']))
            self._event(db, command, 'attempt_reserved', {**payload, 'input_manifest': manifest}, job_id=job_id)
            return {'job_id': job_id, 'attempt_id': attempt_id}
        return self._apply(command, 'reserve_recovery', payload, change)

    def _owned_recovery(self, db, unit, job_id, execution_fence, input_manifest):
        job, current = self._job(db, unit, job_id, execution_fence, input_manifest)
        assignment = db.execute('SELECT * FROM recovery_assignments WHERE job_id=?', (job_id,)).fetchone()
        if job['role'] != 'recovery' or assignment is None or not job['cleanup']:
            raise TransitionError('findings require the cleaned exact recovery assignment')
        failed = dict(db.execute('SELECT j.*,a.input_manifest_json,a.budget_generation AS failed_budget FROM jobs j '
                                'JOIN attempts a ON a.id=j.attempt_id WHERE j.id=?', (assignment['failed_job_id'],)).fetchone())
        expected = recovery_manifest_in(db, failed)
        if input_manifest != expected:
            raise TransitionError('investigation does not identify the exact failed input')
        current = bool(current and not recovery_gates(db, unit, failed['id'], job['member_id'], job['candidate_id'], job_id))
        return job, failed, current

    def _finish(self, db, command, unit, job, failed, current, findings, *, publication=False):
        if not current:
            findings = {**findings, 'incomplete': True}
        db.execute('INSERT INTO recovery_findings VALUES(?,?,?,?,?)',
                   (job['id'], int(findings['incomplete']), int(current), _json(findings), self.clock()))
        self._result(db, command, job, {'failed_job_id': failed['id'], 'findings': findings}, 'investigated' if current else 'stale_investigation')
        db.execute("UPDATE attempt_failures SET investigation='complete',findings_json=? WHERE attempt_id=?",
                   (_json(findings), failed['attempt_id']))
        for gate in db.execute("SELECT id,details_json FROM unit_gates WHERE unit_id=? AND code='recovery_pending' AND resolved_at IS NULL", (unit['id'],)).fetchall():
            if json.loads(gate['details_json']).get('job_id') == failed['id']:
                self._resolve(db, command, gate['id'], {'investigation_job_id': job['id'], 'incomplete': findings['incomplete']})
        approach = False
        implementation = not recovery_gates(db, unit, failed['id'], job['member_id'], job['candidate_id'], job['id'], diagnostic=False)
        if current and not publication and implementation:
            related = db.execute('SELECT f.findings_json FROM attempt_failures f JOIN attempts a ON a.id=f.attempt_id '
                'WHERE f.unit_id=? AND a.budget_generation=? AND EXISTS(SELECT 1 FROM budget_entries b WHERE b.attempt_id=a.id AND b.kind=\'charge\') '
                'AND NOT EXISTS(SELECT 1 FROM budget_entries b WHERE b.attempt_id=a.id AND b.kind=\'correction\') ORDER BY f.position',
                (unit['id'], unit['budget_generation'])).fetchall()
            values = [json.loads(row[0]) if row[0] else {} for row in related]
            pattern = findings['pattern'].strip().casefold()
            repeated = bool(pattern and sum(str(row.get('pattern', '')).strip().casefold() == pattern for row in values) >= 2)
            uncertain = len(values) >= 2 and (not pattern or findings['incomplete'] or bool(findings['uncertainty'].strip()))
            alternative = findings.get('alternative')
            normalize = lambda text: ' '.join(str(text).casefold().split())
            change = normalize(alternative['change']) if alternative else ''
            prior = [normalize(row['alternative'].get('change')) for row in values[:-1] if row.get('alternative')]
            cosmetic = bool(re.search(r'(?:fresh|new) session|re(?:word|phrase).*prompt|stronger model', change))
            concrete = bool(alternative and findings['evidence'] and change not in prior and not cosmetic
                            and not findings['incomplete'] and not findings['uncertainty'].strip())
            if (repeated or uncertain) and not concrete:
                task = db.execute('SELECT 1 FROM job_tasks WHERE job_id=?', (failed['id'],)).fetchone()
                scopes = ([row[0] for row in db.execute('SELECT member_id FROM job_members WHERE job_id=? ORDER BY member_id', (failed['id'],))]
                          if task else [failed['member_id']])
                if not scopes:
                    raise TransitionError('a task investigation requires its complete failed scope')
                for scope in scopes:
                    if not db.execute("SELECT 1 FROM unit_gates WHERE unit_id=? AND code='approach_required' AND member_id IS ? AND resolved_at IS NULL", (unit['id'], scope)).fetchone():
                        self._open_gate(db, command, 'approach_required', {'failed_job_id': failed['id'], 'reason': 'different_approach_required'}, scope)
                approach = True
            if not approach and alternative and not findings['incomplete']:
                db.execute('INSERT INTO unit_context(unit_id,retry_plan_json) VALUES(?,?) ON CONFLICT(unit_id) DO UPDATE SET retry_plan_json=excluded.retry_plan_json',
                           (unit['id'], _json(alternative)))
            db.execute("UPDATE unit_issues SET state='pending' WHERE state='failed' AND EXISTS("
                'SELECT 1 FROM job_members p WHERE p.job_id=? AND p.member_id=unit_issues.id '
                'AND p.requirements_revision_id=unit_issues.requirements_revision_id)', (failed['id'],))
        from .migration_recovery import advance_in
        advance_in(self,db,command,unit)
        return {'accepted': current, 'investigated_job_id': failed['id'], 'approach_required': approach}

    def finish(self, command, job_id, *, execution_fence, input_manifest, findings, incomplete=False, gates=()):
        findings = _findings(findings, incomplete)
        gates = _verification_gates(gates)
        payload = dict(job_id=job_id, execution_fence=execution_fence, input_manifest=input_manifest, findings=findings, gates=gates)
        def change(db, unit):
            job, failed, current = self._owned_recovery(db, unit, job_id, execution_fence, input_manifest)
            answer = self._finish(db, command, unit, job, failed, current, findings)
            if current and gates and not recovery_gates(db, unit, failed['id'], job['member_id'], job['candidate_id'], job_id, diagnostic=False):
                if unit['current_candidate_id'] is None:
                    raise TransitionError('verification requires an exact current candidate')
                for gate in gates:
                    self._open_gate(db, command, gate['code'], gate['details'], candidate_id=unit['current_candidate_id'])
            return answer
        return self._apply(command, 'finish_recovery', payload, change)

    def recover_publication(self, command, job_id, *, execution_fence, input_manifest, findings, candidate, evidence, incomplete=False, gates=()):
        findings = _findings(findings, incomplete)
        gates = _verification_gates(gates)
        if not isinstance(candidate, Candidate) or not isinstance(evidence, dict):
            raise TransitionError('publication recovery requires verified candidate evidence')
        payload = dict(job_id=job_id, execution_fence=execution_fence, input_manifest=input_manifest,
                       findings=findings, candidate=asdict(candidate), evidence=evidence, gates=gates)
        def change(db, unit):
            job, failed, current = self._owned_recovery(db, unit, job_id, execution_fence, input_manifest)
            if not current:
                return {**self._finish(db, command, unit, job, failed, False, findings), 'corrected': False}
            if recovery_gates(db, unit, failed['id'], job['member_id'], job['candidate_id'], job_id, diagnostic=False):
                return {**self._finish(db, command, unit, job, failed, True, findings), 'corrected': False}
            owner = db.execute('SELECT stage FROM builder_assignments WHERE job_id=?',(failed['id'],)).fetchone()
            child = db.execute('SELECT 1 FROM job_tasks WHERE job_id=?',(failed['id'],)).fetchone()
            expected_phase = 'integrating' if owner else 'building'
            if (failed['role'] != 'build' or child or (owner and owner['stage'] != 'integration')
                    or unit['current_candidate_id'] is not None or unit['state'] != expected_phase):
                raise TransitionError('only the current failed publisher can recover its publication')
            original = json.loads(failed['input_manifest_json'])
            if owner:
                from .delivery import Delivery
                expected = Delivery._builder_manifest_in(db,unit,'integration',original.get('base_oid'),original.get('policy_hash'))
                requirements_current = original == expected
                requirements_hash = expected['requirements_hash']
                Delivery._publication_target_in(expected,candidate)
            else:
                member = self._singleton(db, unit)
                requirements_current = member['requirements_revision_id'] == original.get('requirements_revision_id')
                requirements_hash = db.execute('SELECT requirements_hash FROM issue_revisions WHERE id=?', (member['requirements_revision_id'],)).fetchone()[0]
            launch = db.execute('SELECT branch FROM job_launches WHERE job_id=?', (failed['id'],)).fetchone()
            observed = evidence.get('observed_at')
            if (evidence.get('verified') is not True or evidence.get('failed_job_id') != failed['id']
                    or type(observed) not in (int, float) or not self.clock()-60 <= observed <= self.clock()
                    or not requirements_current
                    or candidate.base_oid != original.get('base_oid') or candidate.policy_hash != original.get('policy_hash')
                    or candidate.input_manifest_hash != manifest_hash(original) or candidate.requirements_hash != requirements_hash
                    or launch is None or candidate.branch != launch[0]
                    or any(evidence.get(key) != getattr(candidate, key) for key in ('head_oid','base_oid','pull_number','policy_hash','requirements_hash','input_manifest_hash'))):
                raise TransitionError('publication proof does not match the original failed assignment')
            charge = db.execute("SELECT id FROM budget_entries WHERE attempt_id=? AND kind='charge'", (failed['attempt_id'],)).fetchone()
            if charge is None or db.execute("SELECT 1 FROM budget_entries WHERE attempt_id=? AND kind='correction'", (failed['attempt_id'],)).fetchone():
                raise TransitionError('publication charge is absent or already corrected')
            result = self._finish(db, command, unit, job, failed, True, findings, publication=True)
            db.execute("INSERT INTO budget_entries(id,attempt_id,kind,corrects_id,reason_code,created_at) VALUES(?,?,'correction',?,'verified_publication',?)",
                       (_id(command.request_id,'correction'), failed['attempt_id'], charge[0], self.clock()))
            self._event(db, command, 'budget_corrected', {'charge_id': charge[0], 'evidence': evidence}, job_id=failed['id'])
            if _charges(db, unit) < 3:
                for gate in db.execute("SELECT id FROM unit_gates WHERE unit_id=? AND code='budget_exhausted' AND resolved_at IS NULL", (unit['id'],)).fetchall():
                    self._resolve(db, command, gate[0], {'charge_correction': charge[0]})
            candidate_id = _id(command.request_id, 'candidate')
            db.execute('INSERT INTO candidates(id,unit_id,pull_number,branch,head_oid,base_oid,policy_hash,requirements_hash,membership_generation,input_manifest_hash,accepted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (candidate_id,unit['id'],candidate.pull_number,candidate.branch,candidate.head_oid,candidate.base_oid,candidate.policy_hash,candidate.requirements_hash,
                 unit['membership_generation'],candidate.input_manifest_hash,self.clock()))
            db.execute("INSERT INTO candidate_evidence(id,unit_id,candidate_id,attempt_id,kind,verdict,actor_id,observed_at,details_json) VALUES(?,?,?,?,'publication','pass',?,?,?)",
                (_id(command.request_id,'evidence'),unit['id'],candidate_id,failed['attempt_id'],command.actor_id,self.clock(),_json(evidence)))
            db.execute("UPDATE units SET state='reviewing',current_candidate_id=? WHERE id=?", (candidate_id,unit['id']))
            db.execute("UPDATE unit_issues SET state='built' WHERE unit_id=? AND left_at IS NULL", (unit['id'],))
            self._event(db, command, 'candidate_selected', {'candidate_id':candidate_id,'source':'verified_recovery'},job_id=failed['id'])
            for gate in gates:
                self._open_gate(db, command, gate['code'], gate['details'], candidate_id=candidate_id)
            self._event(db, command, 'phase_changed', {'before':unit['state'],'after':'reviewing'})
            return {**result, 'candidate_id':candidate_id, 'corrected':True}
        return self._apply(command, 'recover_publication', payload, change)


class RecoveryQueries:
    def __init__(self, store):
        self.store = store

    def pending_failures(self, *, after='', limit=50):
        limit = _limit(limit)
        if not isinstance(after, str):
            raise ValueError('recovery cursor must be a job ID')
        with self.store.read_snapshot() as db:
            rows = db.execute("SELECT j.id FROM jobs j JOIN attempt_failures f ON f.attempt_id=j.attempt_id "
                "WHERE j.id>? AND j.cleanup=1 AND f.recovery_required=1 AND f.investigation='pending' "
                'AND NOT EXISTS(SELECT 1 FROM recovery_assignments r WHERE r.failed_job_id=j.id) ORDER BY j.id LIMIT ?',
                (after,limit+1)).fetchall()
        return {'items':[row[0] for row in rows[:limit]],'next_cursor':rows[limit-1][0] if len(rows)>limit else None}

    def results(self, *, after='', limit=50):
        limit = _limit(limit)
        if not isinstance(after, str):
            raise ValueError('recovery cursor must be a job ID')
        with self.store.read_snapshot() as db:
            rows = db.execute('SELECT j.id FROM jobs j JOIN recovery_assignments r ON r.job_id=j.id '
                'JOIN job_execution_state e ON e.job_id=j.id WHERE j.id>? AND j.outcome IS NULL AND e.captured_at IS NOT NULL '
                'ORDER BY j.id LIMIT ?', (after,limit+1)).fetchall()
        return {'items':[row[0] for row in rows[:limit]],'next_cursor':rows[limit-1][0] if len(rows)>limit else None}

    def failure(self, failed_job_id):
        job = ExecutionQueries(self.store).job(failed_job_id)
        with self.store.read_snapshot() as db:
            row = db.execute('SELECT * FROM attempt_failures WHERE attempt_id=? AND recovery_required=1', (job['attempt_id'],)).fetchone()
            if row is None:
                raise KeyError(failed_job_id)
            failure = dict(row)
        failure['findings'] = json.loads(failure.pop('findings_json') or 'null')
        failure['attempt'] = job['id']
        return {**job, 'failure':failure}
