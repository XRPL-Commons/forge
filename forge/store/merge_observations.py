"""Bound mergeability observations before a merge intent is authorized."""
import json
import time
import uuid

from .models import Command, TransitionError
from .transitions import Lifecycle


class MergeObservations(Lifecycle):
    def __init__(self, store, *, clock=time.time, uncertainty_seconds=900):
        super().__init__(store,clock=clock)
        self.uncertainty_seconds=uncertainty_seconds

    def observe(self, unit_id, candidate_id, evidence):
        with self.store.read_snapshot() as db:
            version=db.execute('SELECT version FROM units WHERE id=?',(unit_id,)).fetchone()[0]
        command=Command(str(uuid.uuid4()),'supervisor:merge-observation',unit_id,version)
        changed=[False]
        def change(db,unit):
            candidate=db.execute('SELECT * FROM candidates WHERE id=? AND unit_id=?',(candidate_id,unit_id)).fetchone()
            if (candidate is None or candidate_id!=unit['current_candidate_id'] or unit['state']!='merging'
                or not isinstance(evidence,dict) or evidence.get('verified') is not True
                or evidence.get('state')!='open' or evidence.get('merged') is not False or evidence.get('draft') is not False
                or any(evidence.get(key)!=candidate[key] for key in ('head_oid','base_oid','pull_number'))
                or evidence.get('repository_id')!=unit['repository_id'] or evidence.get('base_branch')!=unit['base_branch']
                or type(evidence.get('observed_at')) not in (int,float) or not self.clock()-60<=evidence['observed_at']<=self.clock()
                or evidence.get('mergeable') is not None and type(evidence['mergeable']) is not bool):
                raise TransitionError('mergeability observation must identify the fresh exact current PR')
            if db.execute("SELECT 1 FROM merge_requests WHERE unit_id=? AND status IN ('pending','uncertain')",(unit_id,)).fetchone():
                return {'pending':'effect_reconciliation'}
            state='ready' if evidence['mergeable'] is True else 'conflict' if evidence['mergeable'] is False else 'unknown'
            db.execute('INSERT INTO candidate_merge_observations VALUES(?,?,?,?) ON CONFLICT(candidate_id) DO UPDATE SET '
                'state=excluded.state,first_at=CASE WHEN candidate_merge_observations.state=excluded.state '
                'THEN candidate_merge_observations.first_at ELSE excluded.first_at END,observed_at=excluded.observed_at',
                (candidate_id,state,evidence['observed_at'],evidence['observed_at']))
            row=db.execute('SELECT * FROM candidate_merge_observations WHERE candidate_id=?',(candidate_id,)).fetchone()
            gates=db.execute("SELECT * FROM unit_gates WHERE unit_id=? AND candidate_id=? AND code='forge_problem' AND resolved_at IS NULL "
                "AND json_extract(details_json,'$.source')='scheduling' AND json_extract(details_json,'$.reason')='mergeability_unknown'",
                (unit_id,candidate_id)).fetchall()
            if state=='ready':
                for gate in gates:
                    self._resolve(db,command,gate['id'],evidence)
                    changed[0]=True
                return {'ready':True}
            if self.clock()-row['first_at']<self.uncertainty_seconds:
                return {'pending':'merge_conflict' if state=='conflict' else 'mergeability_unknown'}
            if state=='conflict':
                self._invalidate(db,command,unit,'merge_conflict',{**evidence,'conflict_since':row['first_at']})
                changed[0]=True
                return {'rebuilding':True}
            if not gates:
                # A different candidate fault retains its own identity and exit.
                existing=db.execute("SELECT id FROM unit_gates WHERE unit_id=? AND candidate_id=? AND code='forge_problem' AND resolved_at IS NULL",
                    (unit_id,candidate_id)).fetchone()
                if existing is None:
                    self._open_gate(db,command,'forge_problem',{'source':'scheduling','reason':'mergeability_unknown',
                        'owner':'forge','candidate_id':candidate_id,'first_at':row['first_at'],'exit':'fresh_mergeability',
                        'observation':evidence},candidate_id=candidate_id)
                    changed[0]=True
            return {'pending':'mergeability_unknown'}
        return self._apply(command,'observe_mergeability',{'candidate_id':candidate_id,'evidence':evidence},change,
            version_changed=lambda:changed[0])
