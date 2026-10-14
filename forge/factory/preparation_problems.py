"""Host-authored launch refusals, separate from worker-controlled capture."""
import json
import re
from ..store.models import TransitionError
from ..store.transitions import manifest_hash


def validate(value):
    if (not isinstance(value, dict) or set(value) != {'version', 'reason', 'gate', 'manifest_digest'}
            or value['version'] != 1 or value['gate'] not in {'dependency', 'environment', 'decision_required'}
            or not isinstance(value['reason'], str) or not re.fullmatch('[a-z_]{1,128}', value['reason'])
            or not isinstance(value['manifest_digest'], str) or not re.fullmatch('[0-9a-f]{64}', value['manifest_digest'])):
        raise TransitionError('invalid host preparation refusal')
    return value


def problem(store, job):
    with store.read_snapshot() as db:
        row = db.execute("SELECT payload_json FROM events WHERE job_id=? AND kind='execution_progress' "
            "AND actor_id='supervisor:execution' AND json_type(payload_json,'$.preparation_refusal')='object' "
            "ORDER BY sequence DESC LIMIT 1", (job['id'],)).fetchone()
    if row is None:
        return None
    payload = json.loads(row[0]); value = validate(payload['preparation_refusal'])
    if payload['execution_fence'] != job['execution_fence'] or value['manifest_digest'] != manifest_hash(job['input_manifest']):
        raise TransitionError('host preparation refusal does not match the assignment')
    return {**value, 'job_id': job['id'], 'owner': 'steward', 'phase': 'preparation'}
