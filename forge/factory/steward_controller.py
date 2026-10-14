"""Recovery, stall and brief are modes of one fenced diagnostic executor."""
import json
import time
from urllib.parse import quote
import uuid

from ..store.db import StoreError
from ..store.executions import ExecutionQueries, Launch
from ..store.models import Command, TransitionError
from ..store.queries import _limit
from ..store.steward import Steward, decision_in, dossier_in, selection_in, trigger_decision_in
from ..store.steward_execution import StewardExecutions, StewardExecutionQueries, StewardMoves
from ..steward_contract import REVERSIBLE, digest
from .config import policy, validate
from .contracts import SHA
from .github import GitHubUnavailable
from .ownership import ownership
from .recovery_controller import RecoveryController
from .runner import ContainerRunner, RunnerError


class StewardController:
    def __init__(self, store, runner, github, *, config, admission, clock=time.time):
        self.store, self.runner, self.github = store, runner, github
        self.config, self.admission, self.clock = validate(config), admission, clock
        self.commands = StewardExecutions(store, clock=clock)
        self.core = Steward(store, clock=clock)
        self.jobs, self.queries = ExecutionQueries(store), StewardExecutionQueries(store)
        self.legacy = RecoveryController(store, runner, github, config=config, admission=admission, clock=clock)

    def _command(self, unit_id):
        with self.store.read_snapshot() as db:
            row = db.execute('SELECT version FROM units WHERE id=?', (unit_id,)).fetchone()
        if row is None:
            raise TransitionError('Unknown steward unit')
        return Command(str(uuid.uuid4()), 'supervisor:steward', unit_id, row[0])

    def _scope(self, unit_id):
        with self.store.read_snapshot() as db:
            if db.execute("SELECT 1 FROM store_metadata WHERE key='import_only' AND value='true'").fetchone():
                raise TransitionError('Imported staging data cannot launch stewards')
            row = db.execute('SELECT * FROM units WHERE id=?', (unit_id,)).fetchone()
            if row is None:
                raise TransitionError('Unknown steward unit')
            unit = dict(row)
            repository = db.execute('SELECT full_name FROM repositories WHERE id=?', (unit['repository_id'],)).fetchone()[0]
            members = [dict(row) for row in db.execute('SELECT m.id AS member_id,m.issue_id AS issue_key,'
                'm.requirements_revision_id,i.github_number AS issue_number,r.requirements_json '
                'FROM unit_issues m JOIN issues i ON i.id=m.issue_id JOIN issue_revisions r ON r.id=m.requirements_revision_id '
                'WHERE m.unit_id=? AND m.left_at IS NULL ORDER BY m.id', (unit_id,))]
            related = [dict(row) for row in db.execute('SELECT i.github_number AS issue_number,r.requirements_json '
                'FROM issues i JOIN issue_revisions r ON r.id=i.current_revision_id '
                "WHERE i.repository_id=? AND i.github_state='open' ORDER BY i.github_number LIMIT 20", (unit['repository_id'],))]
        for row in members+related:
            row['requirements'] = json.loads(row.pop('requirements_json'))
        scope = dict(unit_id=unit_id, unit_version=unit['version'], unit_state=unit['state'],
                     repo_id=unit['repository_id'], repo=repository, base_branch=unit['base_branch'], members=members)
        return unit, scope, related

    def _observe(self, scope, related=(), *, diagnostic=False):
        """GitHub observations precede SQL; reservation rechecks exact local pins."""
        observations, seen = [], set()
        for row in [*scope['members'], *related]:
            number = row['issue_number']
            if number in seen:
                continue
            seen.add(number)
            item = dict(repo=scope['repo'], repo_id=scope['repo_id'], number=number,
                        unit_state=scope['unit_state'], candidate=None)
            remote, revision = self.github.issue(item)
            if ('member_id' in row and not diagnostic and revision != row['requirements']) or remote.get('state') not in {'open', 'closed'}:
                raise TransitionError('Issue observation changed before steward preparation')
            if 'member_id' in row:
                labels = {item['name'] if isinstance(item, dict) else item for item in remote.get('labels', [])}
                if labels.intersection(self.config['hold_labels']) or ownership(remote, item, scope['repo']):
                    raise TransitionError('Independent ownership prevents steward execution')
            if 'member_id' not in row:
                observations.append(dict(repository_id=scope['repo_id'], issue_number=number,
                                         state=remote['state'], revision=revision if isinstance(revision,str) else digest(revision)))
        target = self.github.target(scope)
        if target != scope['base_branch']:
            raise TransitionError('Repository target branch changed')
        head = (self.github.call('controller', scope, 'GET', '/branches/'+quote(target, safe='')).get('commit') or {}).get('sha')
        if not isinstance(head, str) or not SHA.fullmatch(head):
            raise GitHubUnavailable('Steward source commit could not be observed')
        return dict(base_oid=head, observed_at=self.clock(), issues=observations)

    def schedule(self, unit_id, *, mode, trigger):
        from ..store.scheduling import Scheduling
        schedules = Scheduling(self.store, clock=self.clock)
        pending = schedules.pending(unit_id, 'steward')
        if pending:
            return {'unit_id': unit_id, 'pending': pending['reason']}
        try:
            result = self._schedule(unit_id, mode=mode, trigger=trigger)
        except (GitHubUnavailable, RunnerError, TransitionError, StoreError, OSError) as error:
            schedules.record(unit_id, owner='steward', reason=getattr(error, 'code', type(error).__name__))
            raise
        if result.get('reserved'):
            schedules.clear(unit_id, owner='steward')
        elif result.get('pending') in {'cycle_limit','physical_retry_limit'}:
            with self.store.read_snapshot() as db:
                exists = db.execute("SELECT 1 FROM unit_gates WHERE unit_id=? AND code='decision_required' AND subject_key='unit' AND resolved_at IS NULL", (unit_id,)).fetchone()
            if not exists:
                self.commands.limit_wait(self._command(unit_id), trigger_key=result['trigger_key'], reason=result['pending'])
        elif result.get('pending') not in {'active', 'decision_required', 'cycle_limit', 'physical_retry_limit'}:
            schedules.record(unit_id, owner='steward', reason=result['pending'])
        return result

    def _schedule(self, unit_id, *, mode, trigger):
        unit, scope, related = self._scope(unit_id)
        if not self.config['enabled']:
            return {'unit_id': unit_id, 'pending': 'runtime_disabled'}
        if self.admission(scope) is not True:
            return {'unit_id': unit_id, 'pending': 'repository_setup'}
        with self.store.read_snapshot() as db:
            occupied = {row[0] for row in db.execute('SELECT slot FROM job_leases UNION SELECT slot FROM discussion_leases')}
            key = ('failure:'+trigger['failed_job_id'] if mode == 'recovery' else
                   'stall:'+trigger['gate_id'] if mode == 'stall' else 'brief:'+trigger['decision_id'])
            prior = db.execute('SELECT t.*,j.outcome,j.cleanup FROM steward_turns t JOIN jobs j ON j.id=t.job_id '
                               'WHERE t.unit_id=? AND t.trigger_key=? ORDER BY t.cycle DESC,t.physical_attempt DESC LIMIT 1',
                               (unit_id, key)).fetchone()
            from ..store.steward_dossier import authority_in
            if trigger_decision_in(db,unit_id,key,authority_in(db,unit),include_applied=True):
                return {'unit_id':unit_id,'pending':'decision_required'}
            if prior and (not prior['cleanup'] or prior['outcome'] is None):
                return {'unit_id':unit_id,'pending':'active'}
            retry_of = prior['job_id'] if prior and prior['outcome'] == 'deferred' else None
            cycles = db.execute('SELECT count(DISTINCT cycle) FROM steward_turns WHERE unit_id=?', (unit_id,)).fetchone()[0]
            if retry_of and prior['physical_attempt'] >= 3:
                return {'unit_id':unit_id,'pending':'physical_retry_limit','trigger_key':key}
            if not retry_of and cycles >= 3:
                return {'unit_id':unit_id,'pending':'cycle_limit','trigger_key':key}
        slot = next((f'steward-{i+1}' for i in range(self.config['slots']['steward'])
                     if f'steward-{i+1}' not in occupied), None)
        if slot is None:
            return {'unit_id': unit_id, 'pending': 'role_capacity'}
        entry, config = self.legacy._provider()
        if entry is None:
            return {'unit_id': unit_id, 'pending': 'provider_unavailable'}
        observations = self._observe(scope, related, diagnostic=mode == 'recovery')
        runner = ContainerRunner(config, environ=self.runner.environ) if isinstance(self.runner, ContainerRunner) else self.runner
        runner.ready()
        if runner.capacity().get('allowed') is not True:
            return {'unit_id': unit_id, 'pending': 'capacity_unavailable'}
        if self.admission(scope) is not True:
            return {'unit_id': unit_id, 'pending': 'repository_setup'}
        with self.store.read_snapshot() as db:
            candidate = db.execute('SELECT branch FROM candidates WHERE id=?', (unit['current_candidate_id'],)).fetchone()
        launch = Launch(candidate[0] if candidate else 'forge/steward-'+unit_id,
                        self.github.publisher('controller'), entry['provider'],
                        self.clock()+config['timeout_seconds']['steward'], config,
                        policy(config, scope['repo_id'], repository_name=scope['repo']), entry['id'], entry['model'])
        command = Command(str(uuid.uuid4()), 'supervisor:steward', unit_id, unit['version'])
        answer = self.commands.reserve(command, mode=mode, trigger=trigger, observations=observations,
            config=self.config, slot=slot, execution_fence=str(uuid.uuid4()), launch=launch, retry_of=retry_of)
        return {'unit_id': unit_id, 'reserved': True, **answer}

    def finish(self, job_id):
        job = self.jobs.job(job_id)
        if job['role'] != 'steward':
            raise TransitionError('Steward completion requires a steward assignment')
        if job['outcome'] is not None:
            return {'job_id': job_id, 'outcome': job['outcome']}
        if not job['cleanup'] or job['captured_at'] is None:
            return {'job_id': job_id, 'pending': 'cleanup'}
        capture = self.jobs.capture(job_id)
        from .execution_problems import classify, contract_problem
        from ..store.execution_problems import ExecutionProblems
        args = dict(execution_fence=job['execution_fence'], input_manifest=job['input_manifest'])
        from ..store.steward_dossier import authority_in
        with self.store.read_snapshot() as db:
            unit = db.execute('SELECT * FROM units WHERE id=?', (job['unit_id'],)).fetchone()
            stale = (job.get('result_revoked') or dossier_in(db, job_id)['authority'] != authority_in(db, unit))
        if stale:
            return self.commands.discard_stale(self._command(job['unit_id']), job_id, **args)
        from .preparation_problems import problem as preparation_problem
        preparation = preparation_problem(self.store, job)
        if preparation is not None:
            return self.commands.defer_job(self._command(job['unit_id']), job_id, **args,
                                          evidence=preparation, code=preparation['gate'], details=preparation)
        execution = classify(job, capture, self.clock())
        if execution and execution['kind'] == 'forge_problem':
            return ExecutionProblems(self.store, clock=self.clock).defer(self._command(job['unit_id']), job_id,
                                                                        **args, details=execution['details'])
        if execution and execution['kind'] != 'ok':
            detail = execution['details']
            gate = 'provider_unavailable' if execution['kind'] == 'provider_error' else 'decision_required'
            return self.commands.defer_job(self._command(job['unit_id']), job_id, **args, code=gate,
                evidence={'kind': execution['kind'], 'details': detail}, details={**detail, 'source': 'steward_execution'})
        if execution is None:
            return self.commands.defer_job(self._command(job['unit_id']), job_id, **args,
                code='decision_required', evidence={'reason': 'execution_unconfirmed'},
                details={'source': 'steward_execution', 'reason': 'execution_unconfirmed'})
        try:
            from .contracts import result
            output = result(capture.get('result'), job)
        except (ValueError, KeyError, TypeError, TransitionError):
            fault = contract_problem(job, capture)
            if fault is None:
                raise TransitionError('Steward decision failed its output contract')
            return ExecutionProblems(self.store, clock=self.clock).defer(self._command(job['unit_id']), job_id,
                                                                        **args, details=fault)
        answer = self.core.accept(self._command(job['unit_id']), job_id=job_id, decision=output, **args)
        decision_id = answer['decision_id']
        with self.store.read_snapshot() as db:
            dossier = dossier_in(db, job_id)
        if dossier['mode'] == 'brief':
            decision_id = dossier['trigger']['decision_id']
        return {'job_id': job_id, **answer, 'continuation': self.advance(decision_id)}

    def advance(self, decision_id):
        """Recoverable two-phase approval, brief preparation and local application."""
        with self.store.read_snapshot() as db:
            view = selection_in(db, decision_id)
        unit_id = view['dossier']['scope']['unit_id']
        from ..store.steward_dossier import authority_in
        with self.store.read_snapshot() as db:
            current = db.execute('SELECT * FROM units WHERE id=?', (unit_id,)).fetchone()
            unchanged = authority_in(db, current) == view['dossier']['authority']
        if not unchanged and not view['application']:
            return {'decision_id':decision_id,'pending':'authority_changed'}
        if view['application']:
            return {'decision_id': decision_id, 'status': 'applied'}
        with self.store.read_snapshot() as db:
            waiting = db.execute("SELECT 1 FROM unit_gates WHERE unit_id=? AND code='decision_required' AND resolved_at IS NULL AND json_extract(details_json,'$.source')='steward' AND json_extract(details_json,'$.decision_id')=?", (unit_id, decision_id)).fetchone()
        if not waiting:
            self.commands.decision_wait(self._command(unit_id), decision_id)
        if not self.config['enabled']:
            return {'decision_id': decision_id, 'pending': 'runtime_disabled'}
        if view['selection'] is None:
            choice = next(c for c in view['output']['choices'] if c['id'] == view['output']['recommended_choice_id'])
            automatic = (view['dossier']['authority']['autonomy']['policy'] == 'reversible'
                         and choice['action'] in REVERSIBLE and choice['judgement'] == 'obvious')
            if not automatic:
                return {'decision_id': decision_id, 'pending': 'operator_choice'}
            self.core.select(self._command(unit_id), decision_id=decision_id, choice_id=choice['id'],
                             choice_digest=digest(choice), selection='auto')
            with self.store.read_snapshot() as db:
                view = selection_in(db, decision_id)
        choice = view['choice']
        unit, scope, related = self._scope(unit_id)
        if self.admission(scope) is not True:
            return {'decision_id': decision_id, 'pending': 'repository_setup'}
        observations = self._observe(scope, related)
        from ..store.steward_dossier import move_observations
        if move_observations(choice,observations) != move_observations(choice,view['dossier']['facts']['observations']):
            return self.commands.changed_observations(
                Command(str(uuid.uuid4()),'supervisor:steward',unit_id,unit['version']),
                decision_id=decision_id,observations=observations)
        if choice['action'] in {'retry', 'rebuild'} and view['brief'] is None:
            return self.schedule(unit_id, mode='brief', trigger=dict(decision_id=decision_id,
                choice_id=choice['id'], choice_digest=digest(choice)))
        moves = StewardMoves(self.store, clock=self.clock)
        return self.core.apply(Command(str(uuid.uuid4()), 'supervisor:steward', unit_id, unit['version']),
            decision_id=decision_id, apply_move=lambda db, command, current, selected, brief:
                moves.apply_move(db, command, current, selected, brief, decision_id=decision_id,
                                 observations=observations, config=self.config))

    def _unit_step(self, unit_id):
        from ..store.steward_dossier import authority_in
        with self.store.read_snapshot() as db:
            unit = db.execute('SELECT * FROM units WHERE id=?', (unit_id,)).fetchone()
            authority = authority_in(db, unit)
            rows = db.execute('SELECT d.decision_id FROM steward_decisions d JOIN steward_turns t ON t.job_id=d.job_id '
                "WHERE d.unit_id=? AND t.mode!='brief' AND NOT EXISTS(SELECT 1 FROM steward_applications a WHERE a.decision_id=d.decision_id) "
                'ORDER BY t.cycle DESC', (unit_id,)).fetchall()
            decision_id, stale_waits = None, []
            for row in rows:
                if decision_in(db, row[0])[2]['authority'] == authority:
                    decision_id = row[0]; break
                if db.execute("SELECT 1 FROM unit_gates WHERE unit_id=? AND code='decision_required' AND resolved_at IS NULL AND json_extract(details_json,'$.source')='steward' AND json_extract(details_json,'$.decision_id')=?", (unit_id,row[0])).fetchone():
                    stale_waits.append(row[0])
            rows = db.execute("SELECT id,code,details_json FROM unit_gates WHERE unit_id=? AND resolved_at IS NULL "
                              "AND code IN ('recovery_pending','approach_required','forge_problem') ORDER BY created_at,id", (unit_id,)).fetchall()
            trigger = None
            for row in rows:
                detail = json.loads(row['details_json'])
                failed = detail.get('job_id') if row['code'] == 'recovery_pending' else detail.get('failed_job_id')
                candidate = None
                if failed and row['code'] in {'recovery_pending', 'approach_required'}:
                    candidate = ('recovery', {'failed_job_id': failed}, 'failure:'+failed)
                elif detail.get('source') == 'scheduling' and detail.get('reason') == 'stalled':
                    candidate = ('stall', {'gate_id': row['id']}, 'stall:'+row['id'])
                if candidate and not trigger_decision_in(db,unit_id,candidate[2],authority,include_applied=True):
                    trigger = candidate; break
        for stale in stale_waits:
            self.commands.retire_stale_wait(self._command(unit_id), stale)
        if decision_id:
            return self.advance(decision_id)
        if trigger:
            return self.schedule(unit_id, mode=trigger[0], trigger=trigger[1])
        return {'unit_id': unit_id, 'pending': 'no_steward_obligation'}

    def cycle(self, *, after=None, limit=50):
        limit = _limit(limit)
        after = after or {}
        if not isinstance(after, dict) or set(after) - {'results','legacy','units'}:
            raise ValueError('Steward cursor must name results, legacy and units')
        self.legacy._active()
        results = self.queries.results(after=after.get('results',''), limit=limit)
        legacy = self.legacy.queries.results(after=after.get('legacy',''), limit=limit)
        with self.store.read_snapshot() as db:
            rows = db.execute("SELECT id FROM units WHERE id>? AND state NOT IN ('merged','closed','dissolved') ORDER BY id LIMIT ?",
                              (after.get('units',''), limit+1)).fetchall()
        units = {'items':[r[0] for r in rows[:limit]], 'next_cursor': rows[limit-1][0] if len(rows)>limit else None}
        items = []
        for kind, page, operation in [('legacy',legacy,self.legacy.finish),('results',results,self.finish),('units',units,self._unit_step)]:
            for identity in page['items']:
                try:
                    items.append(operation(identity))
                except (GitHubUnavailable, RunnerError, TransitionError, StoreError, OSError) as error:
                    items.append({'id': identity, 'kind': kind, 'error': getattr(error, 'code', type(error).__name__)})
        cursor = {name:page['next_cursor'] or '' for name,page in [('results',results),('legacy',legacy),('units',units)]}
        return {'items': items, 'next_cursor':cursor if any(cursor.values()) else None}
