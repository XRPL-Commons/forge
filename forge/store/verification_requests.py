"""Exact retained verification reports behind compact assignment and gate refs.

Structured result artifacts are durable. References never point at expiring logs
or substitute current task output for the output assigned to this integration.
"""
import hashlib
import json

from .models import TransitionError


def result_in(db, job_id):
    row = db.execute("SELECT content,codec,digest,COALESCE(raw_size,size) AS raw_size FROM job_artifacts "
                     "WHERE job_id=? AND kind='result' AND sequence=0", (job_id,)).fetchone()
    if (row is None or row['codec'] != 'identity' or row['content'] is None
            or len(row['content']) != row['raw_size']
            or hashlib.sha256(row['content']).hexdigest() != row['digest']):
        raise TransitionError('verification result evidence is missing or corrupt')
    return json.loads(row['content']), row['digest']


def requested_in(evidence):
    report = evidence.get('report')
    if not isinstance(report, dict) or report.get('outcome') != 'needs_human_verification':
        return None
    value = report.get('verification')
    if (not isinstance(value, dict) or set(value) not in ({'what', 'why'}, {'what', 'why', 'checks'})
            or not all(isinstance(value[key], str) and value[key].strip() for key in ('what', 'why'))
            or not isinstance(value.get('checks', []), list)
            or any(not isinstance(check, str) or not check.strip() for check in value.get('checks', []))):
        raise TransitionError('retained verification request is malformed')
    return {'checks': [], **value}


def references_in(db, outputs):
    references = []
    for output in outputs:
        job = db.execute('SELECT id FROM jobs WHERE attempt_id=?', (output['attempt_id'],)).fetchone()
        if job is None:
            raise TransitionError('accepted output has no result execution')
        evidence, digest = result_in(db, job['id'])
        if requested_in(evidence) is not None:
            references.append({'output_id': output['id'], 'result_digest': digest})
    return sorted(references, key=lambda row: row['output_id'])


def expand_in(db, manifest, *, unit_id):
    outputs = {ref['output_id']: ref for ref in manifest['part_outputs']}
    requests, seen = [], set()
    for reference in manifest['verification_requests']:
        if (set(reference) != {'output_id', 'result_digest'} or reference['output_id'] in seen
                or reference['output_id'] not in outputs):
            raise TransitionError('verification reference is outside the assigned output set')
        seen.add(reference['output_id'])
        output = db.execute('SELECT p.*,j.id AS job_id FROM part_outputs p JOIN jobs j ON j.attempt_id=p.attempt_id '
                            'WHERE p.id=? AND p.unit_id=?', (reference['output_id'], unit_id)).fetchone()
        if output is None or any(output[key] != value for key, value in outputs[reference['output_id']].items() if key != 'output_id'):
            raise TransitionError('verification output differs from its immutable assignment')
        evidence, digest = result_in(db, output['job_id'])
        request = requested_in(evidence)
        if digest != reference['result_digest'] or request is None:
            raise TransitionError('verification reference does not match the retained result')
        requests.append({**reference, 'verification': request})
    return requests


def gate_details_in(db, gate):
    value = json.loads(gate['details_json'])
    job_id = value.get('verification_job_id')
    if job_id is None:
        return value  # Existing inline verification gates retain their contract.
    row = db.execute("SELECT a.input_manifest_json,c.input_manifest_hash FROM jobs j "
        "JOIN attempts a ON a.id=j.attempt_id JOIN builder_assignments b ON b.job_id=j.id AND b.stage='integration' "
        "JOIN candidate_evidence e ON e.attempt_id=a.id AND e.kind='publication' AND e.verdict='pass' "
        'JOIN candidates c ON c.id=e.candidate_id AND c.unit_id=j.unit_id '
        'WHERE j.id=? AND j.unit_id=? AND c.id=?', (job_id, gate['unit_id'], gate['candidate_id'])).fetchone()
    if row is None:
        raise TransitionError('verification gate is not bound to its candidate publication')
    from .transitions import manifest_hash
    manifest = json.loads(row['input_manifest_json'])
    if manifest_hash(manifest) != row['input_manifest_hash']:
        raise TransitionError('verification candidate input identity changed')
    requests = expand_in(db, manifest, unit_id=gate['unit_id'])
    evidence, _ = result_in(db, job_id)
    final = requested_in(evidence)
    values = [item['verification'] for item in requests] + ([final] if final is not None else [])
    return {**value, 'requests': requests, **({'final_verification': final} if final else {}),
            'what': '\n\n'.join(item['what'] for item in values),
            'why': '\n\n'.join(item['why'] for item in values),
            'checks': sorted({check for item in values for check in item['checks']})}
