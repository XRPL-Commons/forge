"""Bounded operator and diagnostic projections of host publication facts."""
import json


def _claim(value):
    if not isinstance(value, dict):
        return None
    result = {}
    for field in ('code', 'reason', 'limit', 'transient', 'status', 'operation', 'repository_id'):
        item = value.get(field)
        if type(item) in (int, float, bool) or isinstance(item, str) and len(item) <= 256:
            result[field] = item
    paths = value.get('paths')
    if isinstance(paths, list):
        result['paths'] = [item[:512] for item in paths[:10] if isinstance(item, str)]
        result['paths_truncated'] = len(paths) > 10 or any(isinstance(item, str) and len(item) > 512 for item in paths[:10])
    return result


def publication_in(db, job_id):
    job = db.execute('SELECT outcome,unit_id FROM jobs WHERE id=?', (job_id,)).fetchone()
    if job is None:
        return None
    intent = db.execute('SELECT id,status,claim_json,updated_at FROM publication_intents WHERE job_id=?', (job_id,)).fetchone()
    if intent:
        # A durable intent owns this output. Older pre-intent claims no longer
        # describe the state, especially once publication has been confirmed.
        claim = _claim(json.loads(intent['claim_json'])) if intent['claim_json'] and intent['status'] in ('blocked', 'uncertain') else None
        return dict(intent_id=intent['id'], status=intent['status'], updated_at=intent['updated_at'],
                    claim=claim, active=job['outcome'] is None and claim is not None)
    row = db.execute("SELECT payload_json,created_at FROM decisions WHERE command='publication_claim' "
        "AND unit_id=? AND json_extract(payload_json,'$.job_id')=? ORDER BY created_at DESC,id DESC LIMIT 1",
        (job['unit_id'], job_id)).fetchone()
    if row is None:
        return None
    return dict(intent_id=None, status='blocked', updated_at=row['created_at'],
                claim=_claim(json.loads(row['payload_json']).get('claim')), active=job['outcome'] is None)
