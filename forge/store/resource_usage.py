"""Typed resource evidence; no limits, inferred final counters or worker reports."""
import json
import math

from .models import TransitionError

REASONS = {'container_identity_unavailable','container_not_running','engine_stats_unavailable',
           'engine_stats_invalid','engine_api_unsupported','counter_unavailable'}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def observe_in(db, job_id, container_id, observation):
    if observation is None:
        return  # Adapters without telemetry do not manufacture evidence.
    if (not isinstance(observation, dict) or not _number(observation.get('checked_at'))
            or observation.get('status') not in {'available','partial','unavailable'}
            or observation.get('reason') not in REASONS | {None}):
        raise TransitionError('invalid resource observation')
    sample = observation.get('sample')
    status = observation['status']
    if status == 'unavailable':
        if sample is not None or observation['reason'] is None:
            raise TransitionError('unavailable resources require a coded reason')
    else:
        if not isinstance(sample, dict) or sample.get('provenance') != 'docker_engine_stats' or not _number(sample.get('sampled_at')):
            raise TransitionError('resource sample requires engine provenance and timestamp')
        cpu, memory = sample.get('cpu_seconds'), sample.get('memory_bytes')
        if ((cpu is not None and (not _number(cpu) or cpu < 0))
                or (memory is not None and (type(memory) is not int or not 0 <= memory < 2**63))
                or cpu is None and memory is None
                or (status == 'available') != (cpu is not None and memory is not None)
                or observation['reason'] != (None if status == 'available' else 'counter_unavailable')):
            raise TransitionError('invalid measured resource counters')
    previous = db.execute('SELECT * FROM job_resource_observations WHERE job_id=?', (job_id,)).fetchone()
    if previous is not None and previous['container_id'] != container_id:
        raise TransitionError('resource observation belongs to another container')
    # An unavailable attempt cannot rewrite a prior measurement's values/time.
    retained = sample if sample is not None else previous
    db.execute('INSERT INTO job_resource_observations VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET '
        'checked_at=excluded.checked_at,status=excluded.status,reason=excluded.reason,sampled_at=excluded.sampled_at,'
        'cpu_seconds=excluded.cpu_seconds,memory_bytes=excluded.memory_bytes',
        (job_id, container_id, observation['checked_at'], status, observation['reason'],
         retained['sampled_at'] if retained is not None else None,
         retained['cpu_seconds'] if retained is not None else None,
         retained['memory_bytes'] if retained is not None else None))


def wall_usage(execution):
    value = dict(seconds=None, started_at=None, finished_at=None, provenance='docker_inspect', unavailable_reason='container_unavailable')
    if not isinstance(execution, dict):
        return value
    start, end = execution.get('started_at'), execution.get('finished_at')
    value.update(started_at=start if _number(start) else None, finished_at=end if _number(end) else None)
    if execution.get('status') not in {'exited','dead'} or execution.get('running') is not False:
        value['unavailable_reason'] = 'not_finished'
    elif not _number(start) or not _number(end):
        value['unavailable_reason'] = 'timestamps_unavailable'
    elif end < start or not math.isfinite(end-start):
        value['unavailable_reason'] = 'timestamps_invalid'
    else:
        value.update(seconds=end-start, unavailable_reason=None)
    return value


def current_in(db, job_id, *, captured_at=None, execution=None):
    row = db.execute('SELECT * FROM job_resource_observations WHERE job_id=?', (job_id,)).fetchone()
    sample = None
    if row is not None and row['sampled_at'] is not None:
        sample = {key:row[key] for key in ('sampled_at','cpu_seconds','memory_bytes')} | {'provenance':'docker_engine_stats'}
    wall = wall_usage(execution)
    if captured_at is None:
        wall['unavailable_reason'] = 'not_captured'
    return dict(captured_at=captured_at, wall=wall, sample=sample,
                collection={key:row[key] for key in ('checked_at','status','reason')} if row is not None else
                           dict(checked_at=None,status='unavailable',reason='not_measured'))


def capture_in(db, job_id, capture, now):
    execution = capture.get('execution')
    identity = db.execute('SELECT container_id FROM job_execution_state WHERE job_id=?', (job_id,)).fetchone()
    verified = (isinstance(execution, dict) and identity is not None
                and identity['container_id'] is not None and execution.get('id') == identity['container_id'])
    usage = current_in(db, job_id, captured_at=now, execution=execution if verified else None)
    if execution is not None and not verified:
        usage['wall']['unavailable_reason'] = 'container_identity_unavailable'
    db.execute('INSERT INTO job_resource_receipts VALUES(?,?)',
               (job_id,json.dumps(usage,sort_keys=True,separators=(',',':'),allow_nan=False)))


def read_in(db, job_id):
    row = db.execute('SELECT usage_json FROM job_resource_receipts WHERE job_id=?', (job_id,)).fetchone()
    return json.loads(row[0]) if row is not None else current_in(db, job_id)
