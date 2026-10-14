"""Inactive steward transaction helpers. Callbacks use existing core transitions.

Every helper runs inside its caller's Lifecycle._apply transaction. No helper
launches a worker, calls a remote service, resets a budget or grants a verdict.
"""
import json

from ..steward_contract import MAX_CYCLES, REVERSIBLE, decision as validate_decision, digest, encoded, require
from .artifacts import read_in
from .executions import Executions, _artifact
from .models import TransitionError, VersionConflict
from .runtime import writable
from .steward_dossier import authority_in, build_dossier_in, trigger_in
from .transitions import _charges, _id, _unit


def artifact_in(db, job_id, kind):
    return json.loads(read_in(db, job_id, kind).content)


def dossier_in(db, job_id):
    return artifact_in(db, job_id, 'steward_dossier')


def decision_in(db, decision_id):
    row = db.execute('SELECT * FROM steward_decisions WHERE decision_id=?',(decision_id,)).fetchone()
    require(row is not None, 'Unknown steward decision')
    output = artifact_in(db,row['job_id'],'steward_decision')
    require(digest(output) == row['output_digest'], 'Steward output changed')
    return dict(row), output, artifact_in(db,row['job_id'],'steward_dossier')


def trigger_decision_in(db, unit_id, trigger_key, authority, *, include_applied=False):
    """Find current semantic authority, never an arbitrary historical decision."""
    rows = db.execute('SELECT d.decision_id,d.job_id,a.decision_id AS applied '
        'FROM steward_decisions d LEFT JOIN steward_applications a ON a.decision_id=d.decision_id '
        'JOIN steward_turns t ON t.job_id=d.job_id WHERE d.unit_id=? AND d.trigger_key=? '
        'ORDER BY t.cycle DESC', (unit_id, trigger_key))
    for row in rows:
        if (include_applied and row['applied']) or dossier_in(db,row['job_id'])['authority'] == authority:
            return row['decision_id']
    return None


def refreshed_cycle_in(db, unit_id, trigger_key, authority):
    """A changed obligation gets a new cycle; failed physical runs cannot escape their cap."""
    prior = db.execute('SELECT t.job_id,j.cleanup,j.outcome FROM steward_turns t '
        'JOIN jobs j ON j.id=t.job_id WHERE t.unit_id=? AND t.trigger_key=? '
        'ORDER BY t.cycle DESC,t.physical_attempt DESC LIMIT 1', (unit_id,trigger_key)).fetchone()
    if prior is None:
        return True
    if not prior['cleanup'] or dossier_in(db,prior['job_id'])['authority'] == authority:
        return False
    if trigger_decision_in(db,unit_id,trigger_key,authority,include_applied=True):
        return False
    if prior['outcome'] == 'stale_investigation':
        return artifact_in(db,prior['job_id'],'result').get('reason') == 'steward_authority_changed'
    return prior['outcome'] == 'success' and db.execute(
        'SELECT 1 FROM steward_decisions WHERE job_id=?', (prior['job_id'],)).fetchone() is not None


def selection_in(db, decision_id):
    row, output, dossier = decision_in(db, decision_id)
    selected = db.execute('SELECT * FROM steward_selections WHERE decision_id=?', (decision_id,)).fetchone()
    applied = db.execute('SELECT * FROM steward_applications WHERE decision_id=?', (decision_id,)).fetchone()
    choice = next((c for c in output['choices'] if selected and c['id']==selected['choice_id']), None)
    brief, brief_id = output['brief'], None
    if choice and (brief is None or brief['choice_id']!=choice['id']):
        enriched = (applied['brief_decision_id'] if applied else
            trigger_decision_in(db,row['unit_id'],'brief:'+decision_id,authority_in(db,_unit(db,row['unit_id']))))
        if enriched:
            brief = decision_in(db,enriched)[1]['brief']
            brief_id = enriched
        else:
            brief = None
    return {'decision_id':decision_id,'output':output,'dossier':dossier,'choice':choice,'brief':brief,
            'brief_decision_id':brief_id,'selection':dict(selected) if selected else None,
            'application':dict(applied) if applied else None}


class Steward(Executions):
    def _current(self, db, unit, dossier):
        writable(db)
        unit = _unit(db,unit['id'])
        self._live(unit)
        require(dossier['scope']['unit_id'] == unit['id'], 'Dossier belongs to another unit')
        if authority_in(db,unit) != dossier['authority']:
            raise VersionConflict('Steward inputs changed; observe the current work')
        return unit

    def reserve_in(self, db, command, unit, *, job_id, dossier, retry_of=None):
        unit = self._current(db,unit,dossier)
        require(command.unit_id == unit['id'], 'Steward command belongs to another unit')
        require(digest({k:v for k,v in dossier.items() if k!='digest'}) == dossier['digest'], 'Dossier digest changed')
        require(trigger_in(db,unit,dossier['mode'],dossier['trigger']) == dossier['trigger_key'], 'Steward trigger changed')
        job = db.execute('SELECT j.*,a.input_manifest_json FROM jobs j JOIN attempts a ON a.id=j.attempt_id '
            'JOIN job_leases l ON l.job_id=j.id WHERE j.id=? AND j.unit_id=?', (job_id,unit['id'])).fetchone()
        require(job and job['role']=='steward' and not job['fenced'] and not job['cleanup'] and job['outcome'] is None,
                'Steward reservation requires its normal owned execution')
        manifest = json.loads(job['input_manifest_json'])
        require(manifest.get('steward_dossier_digest') == dossier['digest']
                and manifest.get('steward_trigger_key') == dossier['trigger_key'], 'Steward launch does not pin the dossier')
        count = db.execute('SELECT count(DISTINCT cycle) FROM steward_turns WHERE unit_id=?',(unit['id'],)).fetchone()[0]
        cycle, physical = count+1, 1
        if retry_of is None:
            require(refreshed_cycle_in(db,unit['id'],dossier['trigger_key'],dossier['authority']),
                    'Existing trigger requires changed semantic authority or an authorized physical retry')
            require(count < MAX_CYCLES, 'Steward cycle limit reached; operator choice required')
        else:
            prior = db.execute('SELECT t.*,j.cleanup,j.outcome,e.container_id FROM steward_turns t JOIN jobs j ON j.id=t.job_id '
                'JOIN job_execution_state e ON e.job_id=j.id WHERE t.job_id=? AND t.unit_id=?', (retry_of,unit['id'])).fetchone()
            require(prior and prior['cleanup'] and prior['outcome']=='deferred' and prior['trigger_key']==dossier['trigger_key'],
                    'Steward retry requires the cleaned exact deferred turn')
            latest = db.execute('SELECT job_id FROM steward_turns WHERE unit_id=? AND trigger_key=? '
                'ORDER BY cycle DESC,physical_attempt DESC LIMIT 1', (unit['id'],dossier['trigger_key'])).fetchone()
            require(latest and latest[0]==retry_of, 'Steward retry must continue the latest physical attempt')
            from ..factory.execution_problems import classify
            evidence = artifact_in(db,retry_of,'result')
            observed = classify({'id':retry_of,'container_id':prior['container_id']}, artifact_in(db,retry_of,'worker_capture'), self.clock())
            provider = observed and observed['kind']=='provider_error' and evidence.get('kind')=='provider_error'
            before_agent = observed and observed['kind']=='forge_problem' and observed['details'].get('phase')=='before_agent' and evidence.get('reason')=='forge_problem'
            require(provider or before_agent, 'Only trusted provider or pre-agent Forge failure can reuse a cycle')
            cycle = prior['cycle']
            physical = db.execute('SELECT count(*) FROM steward_turns WHERE unit_id=? AND cycle=?',(unit['id'],cycle)).fetchone()[0]+1
            require(physical <= 3, 'Steward physical retry limit reached; owned wait requires operator attention')

        require(not trigger_decision_in(db,unit['id'],dossier['trigger_key'],dossier['authority'],include_applied=True),
                'Trigger already has a current or applied steward decision')
        require(not db.execute('SELECT 1 FROM steward_turns t JOIN jobs j ON j.id=t.job_id '
            'WHERE t.unit_id=? AND t.trigger_key=? AND (j.cleanup=0 OR j.outcome IS NULL)',
            (unit['id'],dossier['trigger_key'])).fetchone(), 'Trigger already has a live steward')
        _artifact(db,job_id,'steward_dossier',encoded(dossier),self.clock())
        db.execute('INSERT INTO steward_turns VALUES(?,?,?,?,?,?,?,?)',
                   (job_id,unit['id'],cycle,dossier['mode'],dossier['trigger_key'],dossier['digest'],self.clock(),physical))
        return {'job_id':job_id,'cycle':cycle,'physical_attempt':physical,'dossier_digest':dossier['digest']}

    def accept_in(self, db, command, unit, *, job_id, execution_fence, input_manifest, decision):
        writable(db)
        job,current = self._job(db,unit,job_id,execution_fence,input_manifest)
        require(current and job['role']=='steward' and job['cleanup'], 'Steward output requires its current cleaned assignment')
        turn = db.execute('SELECT * FROM steward_turns WHERE job_id=? AND unit_id=?',(job_id,unit['id'])).fetchone()
        require(turn is not None, 'Steward job has no pinned turn')
        dossier = artifact_in(db,job_id,'steward_dossier')
        self._current(db,unit,dossier)
        captured = artifact_in(db,job_id,'worker_capture')
        require(captured.get('result') == decision, 'Decision does not match the captured worker output')
        accepted = decision_in(db,dossier['trigger']['decision_id'])[1] if dossier['mode']=='brief' else None
        output = validate_decision(decision,dossier,accepted=accepted)
        require(not trigger_decision_in(db,unit['id'],turn['trigger_key'],dossier['authority'],include_applied=True),
                'Trigger already has a current or applied decision')
        identifier = _id(command.request_id,'decision')
        _artifact(db,job_id,'steward_decision',encoded(output),self.clock())
        db.execute('INSERT INTO steward_decisions VALUES(?,?,?,?,?,?)',
            (identifier,job_id,digest(output),unit['id'],turn['trigger_key'],self.clock()))
        self._result(db,command,job,{'steward_decision_id':identifier,'output_digest':digest(output)},'success')
        return {'decision_id':identifier,'output_digest':digest(output),'mode':dossier['mode'],'accepted':True}

    def accept(self, command, **parameters):
        payload = {key:value for key,value in parameters.items() if key!='decision'}
        payload['decision_digest'] = digest(parameters['decision'])
        return self._apply(command,'accept_steward',payload,
            lambda db,unit:self.accept_in(db,command,unit,**parameters))

    def select_in(self, db, command, unit, *, decision_id, choice_id, choice_digest, selection, on_select=None):
        row,output,dossier = decision_in(db,decision_id)
        unit = self._current(db,unit,dossier)
        require(dossier['mode']!='brief', 'Brief output cannot authorize another move')
        require(selection in {'auto','operator'}, 'Select automatically or as an authenticated operator')
        choices = [choice for choice in output['choices'] if choice['id']==choice_id]
        require(len(choices)==1 and digest(choices[0])==choice_digest, 'Selected steward choice changed')
        choice = choices[0]
        if selection=='auto':
            require(dossier['authority']['autonomy']['policy']=='reversible'
                    and choice['action'] in REVERSIBLE and choice['judgement']=='obvious'
                    and output['recommended_choice_id']==choice_id, 'This choice requires an operator')
        require(not db.execute('SELECT 1 FROM steward_selections WHERE decision_id=?',(decision_id,)).fetchone(),
                'Steward decision is already selected')
        need_brief = choice['action'] in {'retry','rebuild'} and (
            output['brief'] is None or output['brief']['choice_id']!=choice_id)
        if need_brief:
            require(db.execute('SELECT count(DISTINCT cycle) FROM steward_turns WHERE unit_id=?',(unit['id'],)).fetchone()[0] < MAX_CYCLES,
                    'Steward cycle limit leaves no brief invocation')
        db.execute('INSERT INTO steward_selections VALUES(?,?,?,?,?,?)',
            (decision_id,choice_id,choice_digest,_id(command.request_id,'decision'),selection,self.clock()))
        if on_select is not None:
            on_select(db,command,unit,choice,need_brief)
            self._current(db,unit,dossier)
        return {'decision_id':decision_id,'choice_id':choice_id,'choice_digest':choice_digest,
                'status':'selected','needs_brief':need_brief}

    def select(self, command, *, on_select=None, **parameters):
        return self._apply(command,'select_steward',parameters,
            lambda db,unit:self.select_in(db,command,unit,on_select=on_select,**parameters))

    def apply_in(self, db, command, unit, *, decision_id, apply_move):
        require(callable(apply_move), 'Steward application requires a core move adapter')
        row,output,dossier = decision_in(db,decision_id)
        unit = self._current(db,unit,dossier)
        selected = db.execute('SELECT * FROM steward_selections WHERE decision_id=?',(decision_id,)).fetchone()
        require(selected is not None, 'Steward choice has not been selected')
        require(not db.execute('SELECT 1 FROM steward_applications WHERE decision_id=?',(decision_id,)).fetchone(),
                'Steward choice is already applied')
        choice = next(c for c in output['choices'] if c['id']==selected['choice_id'])
        require(digest(choice)==selected['choice_digest'], 'Selected choice digest changed')
        brief,brief_id = output['brief'], None
        if choice['action'] in {'retry','rebuild'} and (brief is None or brief['choice_id']!=choice['id']):
            enriched = trigger_decision_in(db,unit['id'],'brief:'+decision_id,dossier['authority'])
            require(enriched is not None, 'Selected move awaits its brief')
            brief_row,brief_output,brief_dossier = decision_in(db,enriched)
            self._current(db,unit,brief_dossier)
            brief,brief_id = brief_output['brief'], enriched
        require(choice['action'] not in {'retry','rebuild'} or _charges(db,unit)<3,
                'Steward cannot reset an exhausted failure budget')
        require(not db.execute("SELECT 1 FROM publication_intents WHERE unit_id=? AND status NOT IN ('confirmed','superseded')", (unit['id'],)).fetchone(),
                'Steward cannot bypass unsettled publication')
        require(not db.execute("SELECT 1 FROM external_effects WHERE unit_id=? AND status IN ('pending','running','uncertain')", (unit['id'],)).fetchone(),
                'Steward cannot bypass unsettled effects')
        require(not db.execute("SELECT 1 FROM merge_requests WHERE unit_id=? AND status IN ('pending','uncertain')", (unit['id'],)).fetchone(),
                'Steward cannot bypass unsettled merge')
        charges = _charges(db,unit)
        budget = unit['budget_generation']
        gates = {r[0] for r in db.execute('SELECT id FROM unit_gates WHERE unit_id=? AND resolved_at IS NULL',(unit['id'],))}
        allowed = set()
        for gate in db.execute('SELECT id,code,details_json FROM unit_gates WHERE unit_id=? AND resolved_at IS NULL',(unit['id'],)):
            details = json.loads(gate['details_json'])
            if (gate['code']=='decision_required' and details.get('source')=='steward' and details.get('decision_id')==decision_id
                    or dossier['mode']=='stall' and gate['id']==dossier['trigger']['gate_id']
                    or dossier['mode']=='recovery' and (gate['code']=='recovery_pending'
                       and details.get('job_id')==dossier['trigger']['failed_job_id']
                       or gate['code']=='approach_required' and details.get('failed_job_id')==dossier['trigger']['failed_job_id'])):
                allowed.add(gate['id'])
        verdicts = [tuple(r) for r in db.execute("SELECT id,verdict,details_json FROM candidate_evidence WHERE unit_id=? AND kind IN ('review','audit') ORDER BY id",(unit['id'],))]
        result = apply_move(db,command,unit,choice,brief)
        require(isinstance(result,dict), 'Move adapter must return its durable outcome')
        current = _unit(db,unit['id'])
        require(current['budget_generation']==budget and _charges(db,current)==charges, 'Steward move cannot change failure charges or budget')
        remaining = {r[0] for r in db.execute('SELECT id FROM unit_gates WHERE unit_id=? AND resolved_at IS NULL',(unit['id'],))}
        require(gates-allowed <= remaining, 'Steward move cannot clear independent holds')
        require(verdicts == [tuple(r) for r in db.execute("SELECT id,verdict,details_json FROM candidate_evidence WHERE unit_id=? AND kind IN ('review','audit') ORDER BY id",(unit['id'],))],
                'Steward move cannot produce or change independent verdicts')
        db.execute('INSERT INTO steward_applications VALUES(?,?,?,?)',
            (decision_id,_id(command.request_id,'decision'),brief_id,self.clock()))
        return {'decision_id':decision_id,'choice_id':choice['id'],'status':'applied','result':result}

    def apply(self, command, *, decision_id, apply_move):
        return self._apply(command,'apply_steward',{'decision_id':decision_id},
            lambda db,unit:self.apply_in(db,command,unit,decision_id=decision_id,apply_move=apply_move))
