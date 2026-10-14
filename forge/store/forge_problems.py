"""Retry image-owned startup faults only after a qualified replacement is active."""
import hashlib
import json
import math
import re

from .models import TransitionError, VersionConflict
from .transitions import Lifecycle


def repair_in(db, gate, now):
    rows = {row['domain']: row for row in db.execute(
        "SELECT domain,revision,settings_json FROM configuration WHERE subject='instance' AND active=1 AND domain IN ('host.json','runtime.json')")}
    if set(rows) != {'host.json', 'runtime.json'}:
        return None
    host = json.loads(rows['host.json']['settings_json'])
    config = json.loads(rows['runtime.json']['settings_json'])
    completed = host.get('updated_at')
    if type(completed) not in (int, float) or not math.isfinite(completed) or not 0 <= completed <= now:
        return None
    # The qualified replacement may already exist when an old-image job exits.
    # Its immutable identity, not capture-time ordering, establishes replacement.
    faults = list(db.execute("SELECT DISTINCT f.job_id,json_extract(l.runner_config_json,'$.image') AS image,"
        "COALESCE(json_extract(l.runner_config_json,'$.environment_tier'),'standard') AS tier, "
        "json_extract(f.details_json,'$.image_id') AS observed_image "
        'FROM forge_problem_jobs f JOIN unit_issues m ON m.id=f.member_id LEFT JOIN job_launches l ON l.job_id=f.job_id '
        'WHERE f.gate_id=? AND m.unit_id=? AND m.left_at IS NULL ORDER BY f.job_id', (gate['id'],gate['unit_id'])))
    if not faults:
        return None
    old_images = {row['image'] for row in faults}
    old_images.update(row['observed_image'] for row in faults)
    targets = {}
    for fault in faults:
        tier = fault['tier']
        if not fault['image'] or tier not in {'standard', 'full'}:
            return None
        target = config.get('environment_tiers', {}).get(tier, {}).get('image', config.get('image') if tier == 'standard' else None)
        qualified = host.get('image' if tier == 'standard' else 'full_image')
        if (not isinstance(target, str) or not re.fullmatch(r'(?:[A-Za-z0-9._:/-]+@)?sha256:[0-9a-f]{64}', target)
                or target in old_images or target != qualified):
            return None
        targets[tier] = target
    return {'verified': True, 'observed_at': now, 'qualified_at': completed,
        'host_revision': rows['host.json']['revision'], 'runtime_revision': rows['runtime.json']['revision'],
        'replacement_images': targets, 'fault_count': len(faults),
        'faults_digest': hashlib.sha256(json.dumps([dict(row) for row in faults],sort_keys=True).encode()).hexdigest()}


class ForgeProblems(Lifecycle):
    def recheck(self, command, gate_id):
        with self.store.read_snapshot() as db:
            gate = self._gate(db, command, gate_id)
            if gate['resolved_at'] is not None:
                return {'gate_id': gate_id, 'resolved': True}
            source = json.loads(gate['details_json']).get('source')
        if source == 'scheduling':
            from .scheduling import Scheduling
            return Scheduling(self.store, clock=self.clock).recheck(command, gate_id)
        if source == 'execution':
            from .execution_problems import ExecutionProblems
            return ExecutionProblems(self.store, clock=self.clock).recheck(command, gate_id)
        with self.store.read_snapshot() as db:
            gate = self._gate(db, command, gate_id)
            if repair_in(db, gate, self.clock()) is None:
                # An ordinary wait observation creates no new event/version.
                return {'gate_id': gate_id, 'pending': 'qualified_runner_update'}
        def change(db, unit):
            self._live(unit)
            gate = self._gate(db, command, gate_id)
            evidence = repair_in(db, gate, self.clock())
            if gate['resolved_at'] is not None or evidence is None:
                raise VersionConflict('qualified runner replacement changed; recheck')
            self._resolve(db, command, gate_id, evidence)
            return {'gate_id': gate_id, 'resolved': True}
        return self._apply(command, 'recheck_forge_problem', {'gate_id': gate_id}, change)

    @staticmethod
    def _gate(db, command, gate_id):
        gate = db.execute("SELECT * FROM unit_gates WHERE id=? AND unit_id=? AND code='forge_problem'",
                          (gate_id, command.unit_id)).fetchone()
        if gate is None:
            raise TransitionError('Forge problem does not belong to this unit')
        return gate


def member_faults_in(db, unit_id, member_id):
    return [dict(row) for row in db.execute('SELECT f.job_id,f.details_json FROM forge_problem_jobs f '
        "JOIN unit_gates g ON g.id=f.gate_id WHERE g.unit_id=? AND g.resolved_at IS NULL AND f.member_id=? ORDER BY f.job_id",
        (unit_id,member_id))]


def inherit_faults_in(lifecycle, db, command, member_id, faults):
    """Carry blocking evidence to a successor membership, never execution pins."""
    if not faults:
        return
    gate_id=lifecycle._open_gate(db,command,'forge_problem',json.loads(faults[0]['details_json']))
    db.executemany('INSERT INTO forge_problem_jobs(job_id,gate_id,member_id,details_json) VALUES(?,?,?,?)',
        [(fault['job_id'],gate_id,member_id,fault['details_json']) for fault in faults])
