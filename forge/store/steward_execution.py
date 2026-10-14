"""Physical steward jobs use normal leases; choices never bypass delivery guards."""
import json

from .executions import Executions, Launch, insert_launch
from .models import TransitionError
from .queries import _limit
from .transitions import _id, _json, applicable_gates


def execution_gates_in(db, unit, dossier, *, job_id=None, diagnostic=True):
    """Ignore only the exact obligation this diagnostic invocation owns."""
    mode, trigger = dossier['mode'], dossier['trigger']
    selected_id = trigger.get('decision_id') if mode == 'brief' else None
    if selected_id:
        from .steward import decision_in
        original = decision_in(db, selected_id)[2]
        mode, trigger = original['mode'], original['trigger']
    seen, blocked = set(), []
    for pin in dossier['authority']['members']:
        for gate in applicable_gates(db, unit, pin['member_id'], unit['current_candidate_id'], job_id=job_id or trigger.get('failed_job_id')):
            key = (gate['id'], gate['code'], gate.get('job_id'), gate.get('member_id'))
            if key in seen:
                continue
            seen.add(key)
            details = json.loads(gate['details_json'])
            if diagnostic and mode == 'recovery' and (
                    gate['code'] == 'external_change' and gate['id'] is None and details.get('reason') == 'issue_closed'
                    or gate['code'] == 'membership_decision' and details.get('reason') in {'member_closed','requirements_changed','requirements_revision_changed'}
                    or gate['code'] == 'dependency' and gate['id'] is None and details.get('reason') == 'prerequisite_output_pending'):
                continue
            if gate['code'] == 'budget_exhausted':
                continue  # Diagnostics do not allocate another implementation attempt.
            if job_id and gate['code'] in {'cleanup_pending', 'result_pending'} and details.get('job_id') == job_id:
                continue
            if mode == 'recovery' and (
                    gate['code'] == 'recovery_pending' and details.get('job_id') == trigger['failed_job_id']
                    or gate['code'] == 'approach_required' and details.get('failed_job_id') == trigger['failed_job_id']):
                continue
            if mode == 'stall' and gate['id'] == trigger['gate_id']:
                continue
            if (selected_id and gate['code'] == 'decision_required' and details.get('source') == 'steward'
                    and details.get('decision_id') == selected_id):
                continue
            blocked.append(gate)
    # A replacement physical job inherits the exact trigger's unrepaired faults.
    # Job-scoped gates are otherwise excluded when admission names its new job ID.
    for row in db.execute("SELECT g.* FROM unit_gates g JOIN steward_turns t ON t.job_id=g.job_id "
                          "WHERE t.unit_id=? AND t.trigger_key=? AND g.code='forge_problem' AND g.resolved_at IS NULL",
                          (unit['id'], dossier['trigger_key'])):
        if not any(gate['id'] == row['id'] for gate in blocked):
            blocked.append(dict(row))
    return blocked


class StewardExecutions(Executions):
    def reserve(self, command, *, mode, trigger, observations, config, slot, execution_fence, launch, retry_of=None):
        from ..factory.contracts import SHA
        from .steward import Steward
        from .steward_dossier import build_dossier_in
        if not isinstance(launch, Launch) or not slot or not execution_fence:
            raise TransitionError('Steward reservation needs an exact physical lease and launch')
        base = observations.get('base_oid') if isinstance(observations, dict) else None
        if not isinstance(base, str) or not SHA.fullmatch(base):
            raise TransitionError('Steward source requires the observed target commit')
        payload = dict(mode=mode, trigger=trigger, observations=observations, config=config,
                       slot=slot, execution_fence=execution_fence, launch=launch.payload(), retry_of=retry_of)

        def change(db, unit):
            self._live(unit)
            dossier = build_dossier_in(db, unit, mode=mode, trigger=trigger, now=self.clock(),
                                      observations=observations, config=config)
            if retry_of:
                from .steward import dossier_in
                previous = dossier_in(db, retry_of)
                if previous['facts']['observations']['base_oid'] != base or previous['authority']['candidate'] != dossier['authority']['candidate']:
                    raise TransitionError('Deferred steward source pins changed')
            if execution_gates_in(db, unit, dossier):
                raise TransitionError('An independent gate blocks this steward invocation')
            attempt_id, job_id = _id(command.request_id, 'attempt'), _id(command.request_id, 'job')
            manifest = dict(steward_dossier_digest=dossier['digest'],
                            steward_trigger_key=dossier['trigger_key'], base_oid=base)
            candidate = dossier['authority']['candidate']
            if candidate:
                manifest['head_oid'] = candidate['head_oid']
            db.execute('INSERT INTO attempts(id,unit_id,member_id,membership_generation,assignment_generation,'
                       'input_manifest_json,budget_generation,created_at) VALUES(?,?,NULL,?,?,?,?,?)',
                       (attempt_id, unit['id'], unit['membership_generation'], unit['fencing_generation'],
                        _json(manifest), unit['budget_generation'], self.clock()))
            db.execute("INSERT INTO jobs(id,unit_id,attempt_id,candidate_id,role,phase,created_at) "
                       "VALUES(?,?,?,?,'steward','reserved',?)",
                       (job_id, unit['id'], attempt_id, unit['current_candidate_id'], self.clock()))
            pins = dossier['authority']['members']
            if not pins:
                raise TransitionError('Steward requires immutable issue membership')
            # The normal jobs_pin_members trigger freezes the current issue scope.
            actual = [dict(row) for row in db.execute('SELECT member_id,requirements_revision_id FROM job_members WHERE job_id=? ORDER BY member_id', (job_id,))]
            if actual != [{key:pin[key] for key in ('member_id','requirements_revision_id')} for pin in pins]:
                raise TransitionError('Steward job scope differs from its frozen dossier')
            db.execute('INSERT INTO job_leases(slot,job_id,execution_fence,acquired_at) VALUES(?,?,?,?)',
                       (slot, job_id, execution_fence, self.clock()))
            insert_launch(db, job_id, slot, execution_fence, payload['launch'], self.clock())
            Steward(self.store, clock=self.clock).reserve_in(db, command, unit, job_id=job_id, dossier=dossier, retry_of=retry_of)
            if mode == 'recovery':
                db.execute('UPDATE attempt_failures SET investigation=? WHERE attempt_id='
                           '(SELECT attempt_id FROM jobs WHERE id=?)', (job_id, trigger['failed_job_id']))
            self._event(db, command, 'attempt_reserved',
                        {'mode': mode, 'trigger': trigger, 'input_manifest': manifest}, job_id=job_id)
            return {'job_id': job_id, 'attempt_id': attempt_id, 'dossier_id': dossier['id']}

        return self._apply(command, 'reserve_steward', payload, change)


    def decision_wait(self, command, decision_id):
        from .steward import decision_in
        def change(db, unit):
            row, _, dossier = decision_in(db, decision_id)
            if row['unit_id'] != unit['id'] or dossier['mode'] == 'brief':
                raise TransitionError('Choice card must belong to this delivery')
            existing = db.execute("SELECT id,details_json FROM unit_gates WHERE unit_id=? AND code='decision_required' AND subject_key='unit' AND resolved_at IS NULL", (unit['id'],)).fetchone()
            if existing:
                details = json.loads(existing['details_json'])
                if details.get('source') != 'steward' or details.get('decision_id') != decision_id:
                    raise TransitionError('An independent decision already owns this wait')
                return {'gate_id': existing['id']}
            gate_id = self._open_gate(db, command, 'decision_required',
                                     {'source': 'steward', 'decision_id': decision_id})
            return {'gate_id': gate_id}
        return self._apply(command, 'steward_decision_wait', {'decision_id': decision_id}, change)


    def retire_stale_wait(self, command, decision_id):
        from .steward import decision_in
        from .steward_dossier import authority_in
        def change(db, unit):
            row, _, dossier = decision_in(db, decision_id)
            if row['unit_id'] != unit['id'] or dossier['authority'] == authority_in(db, unit):
                raise TransitionError('Current steward choice cannot be retired')
            ids = [r[0] for r in db.execute("SELECT id FROM unit_gates WHERE unit_id=? AND code='decision_required' AND resolved_at IS NULL AND json_extract(details_json,'$.source')='steward' AND json_extract(details_json,'$.decision_id')=?", (unit['id'], decision_id))]
            for gate_id in ids:
                self._resolve(db, command, gate_id, {'reason':'steward_authority_changed','decision_id':decision_id})
            return {'decision_id':decision_id,'retired_waits':ids}
        return self._apply(command, 'retire_steward_wait', {'decision_id':decision_id}, change)

    def limit_wait(self, command, *, trigger_key, reason):
        if reason not in {'cycle_limit', 'physical_retry_limit'}:
            raise TransitionError('Unknown steward execution limit')
        def change(db, unit):
            existing = db.execute("SELECT id FROM unit_gates WHERE unit_id=? AND code='decision_required' AND subject_key='unit' AND resolved_at IS NULL", (unit['id'],)).fetchone()
            if existing:
                return {'gate_id': existing['id']}
            return {'gate_id': self._open_gate(db, command, 'decision_required',
                {'source': 'steward_execution', 'reason': reason, 'trigger_key': trigger_key})}
        return self._apply(command, 'steward_limit_wait', dict(trigger_key=trigger_key,reason=reason), change)

    def discard_stale(self, command, job_id, *, execution_fence, input_manifest):
        from .steward import dossier_in
        from .steward_dossier import authority_in
        def change(db, unit):
            job, current = self._job(db, unit, job_id, execution_fence, input_manifest)
            if job['role'] != 'steward' or not job['cleanup']:
                raise TransitionError('Only a cleaned steward result can become stale')
            if current and dossier_in(db, job_id)['authority'] == authority_in(db, unit):
                raise TransitionError('Current steward output cannot be discarded')
            self._result(db, command, job, {'reason': 'steward_authority_changed'}, 'stale_investigation')
            return {'job_id': job_id, 'accepted': False, 'outcome': 'stale_investigation'}
        return self._apply(command, 'discard_steward', dict(job_id=job_id, execution_fence=execution_fence,
                                                        input_manifest=input_manifest), change)


class StewardExecutionQueries:
    def __init__(self, store):
        self.store = store

    def results(self, *, after='', limit=50):
        limit = _limit(limit)
        with self.store.read_snapshot() as db:
            rows = db.execute("SELECT j.id FROM jobs j JOIN steward_turns t ON t.job_id=j.id "
                "JOIN job_execution_state e ON e.job_id=j.id WHERE j.id>? AND j.outcome IS NULL "
                'AND j.cleanup=1 AND e.captured_at IS NOT NULL ORDER BY j.id LIMIT ?', (after, limit+1)).fetchall()
        return {'items': [row[0] for row in rows[:limit]], 'next_cursor': rows[limit-1][0] if len(rows)>limit else None}


def continuation_pins_in(db, unit):
    return {key: unit[key] for key in ('fencing_generation', 'membership_generation', 'budget_generation',
                                      'current_candidate_id')} | {
        'members': [dict(row) for row in db.execute('SELECT id,requirements_revision_id FROM unit_issues '
            'WHERE unit_id=? AND left_at IS NULL ORDER BY id', (unit['id'],))]}


def retry_plan_in(db, unit, role):
    """A projection is usable only after its exact core application committed."""
    row = db.execute('SELECT retry_plan_json FROM unit_context WHERE unit_id=?', (unit['id'],)).fetchone()
    plan = json.loads(row[0]) if row and row[0] else None
    if not isinstance(plan, dict) or plan.get('source') != 'steward' or plan.get('role') != role:
        return None
    if plan.get('pins') != continuation_pins_in(db, unit):
        return None
    if not db.execute('SELECT 1 FROM steward_applications WHERE decision_id=?', (plan['decision_id'],)).fetchone():
        raise TransitionError('Retry projection has no committed steward application')
    return plan


def retry_target_in(db, dossier, action):
    failed_id = dossier['trigger'].get('failed_job_id')
    if not failed_id or action == 'rebuild':
        return None
    task = db.execute('SELECT plan_id,task_id FROM job_tasks WHERE job_id=?', (failed_id,)).fetchone()
    if task:
        return {'stage':'task', **dict(task)}
    builder = db.execute('SELECT stage FROM builder_assignments WHERE job_id=?', (failed_id,)).fetchone()
    return dict(builder) if builder else None


def retry_target_matches(plan, assignment):
    target = plan.get('target')
    if target is None:
        return True
    return assignment is not None and all(assignment.get(key) == value for key,value in target.items())


def task_retry_in(db, unit, plan_id, task_id):
    """One new physical task assignment per applied, exact steward choice."""
    plan = retry_plan_in(db, unit, 'build')
    target = {'stage': 'task', 'plan_id': plan_id, 'task_id': task_id}
    if not plan or plan.get('target') != target:
        return None
    used = db.execute("SELECT 1 FROM job_launches WHERE json_extract(context_json, '$.steward_retry.decision_id')=?",
                      (plan['decision_id'],)).fetchone()
    return None if used else plan


def retry_plan_for_job_in(db, unit, job):
    """Read frozen assignment instructions, never a later unit-wide choice."""
    from .steward import selection_in
    plan = job.get('steward_retry')
    if plan is None:
        return None
    if not isinstance(plan,dict) or plan.get('source') != 'steward':
        raise TransitionError('Invalid steward continuation context')
    selected = selection_in(db, plan['decision_id'])
    dossier, choice = selected['dossier'], selected['choice']
    if (not selected['application'] or not choice or dossier['scope']['unit_id'] != job['unit_id']
            or plan['action'] != choice['action'] or plan['parameters'] != choice['parameters']
            or plan['brief'] != selected['brief'] or job['role'] != plan['role']
            or plan.get('target') != retry_target_in(db,dossier,choice['action'])):
        raise TransitionError('Continuation differs from its exact applied steward choice')
    pins = plan['pins']
    if (job['assignment_generation'] != pins['fencing_generation']
            or job['assigned_membership'] != pins['membership_generation']
            or job['assigned_budget'] != pins['budget_generation']
            or job['input_manifest'].get('base_oid') != plan['base_oid']):
        raise TransitionError('Continuation assignment pins changed')
    members = {row['id']:row['requirements_revision_id'] for row in pins['members']}
    if any(members.get(row['member_id']) != row['requirements_revision_id'] for row in job['members']):
        raise TransitionError('Continuation issue scope changed')
    assignment = {'stage':job.get('builder_stage')}
    if job.get('task'):
        assignment.update(task_id=job['task_id'],plan_id=job['task']['plan_id'])
    if not retry_target_matches(plan,assignment):
        raise TransitionError('Continuation belongs to another task')
    return plan


class StewardMoves(Executions):
    def apply_move(self, db, command, unit, choice, brief, *, decision_id, observations, config):
        """Local moves and explicitly approved intents share one authority boundary."""
        from .steward import decision_in
        from .transitions import _unit, _charges
        _, _, dossier = decision_in(db, decision_id)
        self._live(unit)
        self._quiescent(db, unit['id'])
        # The steward owns its original trigger and its own choice card only.
        for gate in execution_gates_in(db, unit, dossier, diagnostic=False):
            details = json.loads(gate['details_json'])
            if gate['code'] != 'decision_required' or details.get('source') != 'steward' or details.get('decision_id') != decision_id:
                raise TransitionError('An independent hold prevents this move')
        parameters = choice['parameters']
        if choice['action'] in {'retry', 'rebuild'} and _charges(db, unit) >= 3:
            raise TransitionError('Steward retry cannot reset the failure budget')
        if observations['base_oid'] != dossier['facts']['observations']['base_oid']:
            raise TransitionError('Target commit changed before applying the steward choice')
        if choice['action'] not in {'retry','wait_on','rebuild'}:
            selected = db.execute('SELECT selected_by FROM steward_selections WHERE decision_id=?', (decision_id,)).fetchone()
            if selected is None or selected[0] != 'operator':
                raise TransitionError('External steward actions require exact operator selection')
            from .steward_effects import queue_in
            return queue_in(db, command, unit, decision_id=decision_id, choice=choice, dossier=dossier)
        if choice['action'] == 'rebuild':
            if parameters['base_oid'] != observations['base_oid']:
                raise TransitionError('Rebuild must use the exact observed target commit')
            self._invalidate(db, command, unit, 'base_changed', {'base_oid': parameters['base_oid'],
                                                               'steward_decision_id': decision_id})
        elif choice['action'] == 'retry':
            if 'provider_id' in parameters and parameters['provider_id'] not in {p['id'] for p in config['provider_pool']}:
                raise TransitionError('Selected provider is no longer configured')
            if 'timeout_seconds' in parameters and not 30 <= parameters['timeout_seconds'] <= 3000:
                raise TransitionError('Retry exceeds the supported execution deadline')
            if 'environment_id' in parameters and parameters['environment_id'] not in config['environment_tiers']:
                raise TransitionError('Selected environment is no longer configured')
            if 'resume' in parameters:
                resume = parameters['resume']
                candidate = dossier['authority']['candidate']
                if not candidate or resume['head_oid'] != candidate['head_oid']:
                    raise TransitionError('Resume needs an exact retained candidate')
        else:
            from .repository_requirements import order_in, orders_in
            matches = [row for row in observations['issues'] if row['repository_id'] == parameters['repository_id']
                       and row['issue_number'] == parameters['issue_number'] and row['state'] == 'open']
            if len(matches) != 1:
                raise TransitionError('Wait requires a fresh open prerequisite observation')
            repository = db.execute('SELECT full_name FROM repositories WHERE id=?', (parameters['repository_id'],)).fetchone()
            if repository is None:
                raise TransitionError('Prerequisite repository is not known')
            target = repository[0].lower()+'#'+str(parameters['issue_number'])
            for member in db.execute('SELECT * FROM unit_issues WHERE unit_id=? AND left_at IS NULL ORDER BY id', (unit['id'],)).fetchall():
                existing = orders_in(db, member['issue_id'], member['requirements_revision_id'])
                order_in(db, issue_id=member['issue_id'], requirements_revision_id=member['requirements_revision_id'],
                    prerequisites=sorted(set(existing+[target])), actor_id=command.actor_id,
                    request_id=_id(command.request_id, 'steward-prerequisite:'+member['id']))
        trigger = dossier['trigger']
        failed_id = trigger.get('failed_job_id')
        if failed_id:
            db.execute("UPDATE attempt_failures SET investigation='complete' WHERE attempt_id="
                       '(SELECT attempt_id FROM jobs WHERE id=?)', (failed_id,))
            db.execute("UPDATE unit_issues SET state='pending' WHERE state='failed' AND EXISTS("
                       'SELECT 1 FROM job_members p WHERE p.job_id=? AND p.member_id=unit_issues.id '
                       'AND p.requirements_revision_id=unit_issues.requirements_revision_id)', (failed_id,))
        for gate in db.execute('SELECT * FROM unit_gates WHERE unit_id=? AND resolved_at IS NULL', (unit['id'],)).fetchall():
            details = json.loads(gate['details_json'])
            owned = (gate['code'] == 'decision_required' and details.get('source') == 'steward'
                     and details.get('decision_id') == decision_id)
            owned |= bool(failed_id and (gate['code'] == 'recovery_pending' and details.get('job_id') == failed_id
                            or gate['code'] == 'approach_required' and details.get('failed_job_id') == failed_id))
            owned |= dossier['mode'] == 'stall' and gate['id'] == trigger['gate_id']
            if owned:
                self._resolve(db, command, gate['id'], {'steward_decision_id': decision_id, 'choice_id': choice['id']})
        if choice['action'] in {'retry', 'rebuild'}:
            unit = _unit(db, unit['id'])
            failure = db.execute('SELECT f.retry_stage FROM attempt_failures f JOIN jobs j ON j.attempt_id=f.attempt_id '
                                 'WHERE j.id=?', (failed_id,)).fetchone() if failed_id else None
            role = failure[0] if failure else {'queued': 'build', 'building': 'build', 'integrating': 'build',
                                               'reviewing': 'review', 'auditing': 'audit'}.get(unit['state'])
            if choice['action'] == 'rebuild':
                role = 'build'
            if role not in {'build', 'review', 'audit'}:
                raise TransitionError('Steward continuation has no implementation role')
            plan = dict(source='steward', decision_id=decision_id, role=role, action=choice['action'],
                        parameters=parameters, brief=brief, pins=continuation_pins_in(db, unit),
                        target=retry_target_in(db,dossier,choice['action']), base_oid=observations['base_oid'])
            db.execute('INSERT INTO unit_context(unit_id,retry_plan_json) VALUES(?,?) '
                       'ON CONFLICT(unit_id) DO UPDATE SET retry_plan_json=excluded.retry_plan_json', (unit['id'], _json(plan)))
        return {'action': choice['action'], 'choice_id': choice['id'], 'budget_generation': unit['budget_generation']}
