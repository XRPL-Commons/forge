"""Typed, candidate-bound progress for human pull-request checks.

Worker prose is never copied into GitHub progress. Publication identities are
retained across progress intents; the intents themselves remain immutable.
"""
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .transitions import applicable_gates


def details_url(origin, repository_id, number):
    if not isinstance(origin, str) or type(repository_id) is not int or repository_id < 1 or type(number) is not int or number < 1:
        return None
    try:
        parsed = urlsplit(origin)
        valid = (parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password
                 and not parsed.path and not parsed.query and not parsed.fragment
                 and re.fullmatch(r'[a-z0-9.-]+(?::[0-9]{1,5})?', parsed.netloc)
                 and (parsed.port is None or 0 < parsed.port < 65536)
                 and not any(ord(c) <= 32 or ord(c) == 127 for c in origin))
    except ValueError:
        return None
    return f'{origin}/pull-requests/{repository_id}/{number}' if valid else None


def publication_key(db, candidate_id):
    row = db.execute("SELECT e.id,e.payload_json FROM external_effects e JOIN candidate_effects c ON c.effect_id=e.id "
        "WHERE c.candidate_id=? AND e.kind='github_check' AND json_extract(e.payload_json,'$.role')='controller' "
        "ORDER BY e.rowid LIMIT 1", (candidate_id,)).fetchone()
    return (json.loads(row['payload_json']).get('check_external_id') or row['id']) if row else None


from .status import REVIEW_CAUSES as CAUSES


def projection(db, unit):
    candidate_id = unit['current_candidate_id']
    if not candidate_id or unit['state'] in {'closed', 'merged'}:
        return None
    # Once a final outcome is queued, progress must never supersede it.
    terminal = db.execute("SELECT 1 FROM external_effects e JOIN candidate_effects c ON c.effect_id=e.id "
        "WHERE c.candidate_id=? AND e.kind='github_check' AND json_extract(e.payload_json,'$.role')='controller' "
        "AND json_extract(e.payload_json,'$.conclusion') IS NOT NULL AND e.status!='superseded' LIMIT 1", (candidate_id,)).fetchone()
    if terminal:
        return None
    gates = applicable_gates(db, unit, candidate_id=candidate_id)
    visible = [g for g in gates if json.loads(g['details_json']).get('source') != 'review_pull_policy'
               and g['code'] not in {'cleanup_pending', 'result_pending'}]
    role = 'audit' if unit['state'] in {'auditing', 'merging'} else 'review'
    value = {'state': 'queued', 'role': role, 'cause': None, 'retry_at': None}
    if visible:
        gate = visible[0]
        detail = json.loads(gate['details_json'])
        cause = detail.get('cause') or detail.get('reason') or gate['code']
        # The typed vocabulary is bounded; unrecognized values never escape.
        cause = cause if cause in CAUSES else gate['code'] if gate['code'] in CAUSES else 'environment'
        retry_at = detail.get('retry_at')
        value.update(state='retry_wait' if type(retry_at) in (int, float) and 0 < retry_at < 253402300800 else 'blocked',
                     cause=cause)
        if value['state'] == 'retry_wait':
            value['retry_at'] = retry_at
    else:
        job = db.execute("SELECT j.phase FROM jobs j JOIN job_leases l ON l.job_id=j.id WHERE j.unit_id=? AND j.candidate_id=? AND j.outcome IS NULL "
                         "AND j.role IN ('review','audit') AND j.fenced=0 AND j.cleanup=0 ORDER BY j.created_at DESC,j.rowid DESC LIMIT 1",
                         (unit['id'], candidate_id)).fetchone()
        if job and job['phase'] == 'running':
            value['state'] = 'running'
        elif unit['state'] == 'merging':
            value['state'] = 'finalizing'
    return value


def summary(value):
    role = value['role'].title()
    state = value['state']
    if state == 'queued':
        return f'Forge {role} is queued for this exact pull request revision.'
    if state == 'running':
        return f'Forge {role} is running for this exact pull request revision.'
    if state == 'finalizing':
        return 'Forge Review and Audit finished; the supervisor is checking the final requirements.'
    text = f'Forge {role} is waiting. ' + CAUSES[value['cause']]
    if value['retry_at'] is not None:
        when = datetime.fromtimestamp(value['retry_at'], timezone.utc).isoformat().replace('+00:00', 'Z')
        text += f' Next automatic retry: {when}.'
    else:
        text += ' Operator attention is needed before work can continue.'
    return text
