"""Bounded operator projections for review-only pull requests, without issue aliases."""
import json
import math

from ..store.status import describe, describe_gate
from ..store.transitions import applicable_gates


# A retargeted/reopened pull keeps its old units for evidence. Only the newest
# binding represents that pull on the board and at its stable detail URL.
LATEST = """p.unit_id=(SELECT newer.unit_id FROM review_pull_requests newer
    WHERE newer.repository_id=p.repository_id AND newer.pull_number=p.pull_number
    ORDER BY newer.created_at DESC,newer.unit_id DESC LIMIT 1)"""


def latest_in(db, repo_id, number):
    row = db.execute('SELECT u.* FROM review_pull_requests p JOIN units u ON u.id=p.unit_id '
        'WHERE p.repository_id=? AND p.pull_number=? ORDER BY p.created_at DESC,p.unit_id DESC LIMIT 1',
        (repo_id, number)).fetchone()
    if row is None:
        raise KeyError('Pull request work is unavailable')
    return dict(row)


def _reason(gate, now):
    details = json.loads(gate['details_json'])
    code = gate['code']
    if code == 'review_source':
        reason = details.get('reason')
        if reason == 'review_unavailable':
            return describe_gate(gate)
        return {'technical_rejection': 'Forge requested changes on this pull request revision.',
                'verdict_failed': 'Forge requested changes on this pull request revision.',
                'pull_draft': 'Waiting for the pull request to leave draft.',
                'label_hold': 'A GitHub label holds this pull request.',
                'repository_disabled': 'This repository is disabled; Forge work is paused.'}.get(reason, describe_gate(gate))
    if code == 'observation_unavailable' and details.get('source') == 'review_pull_policy':
        return 'Publishing the Forge check before starting review.'
    return describe_gate(gate)


def row_in(db, unit, now):
    binding = db.execute('SELECT p.*,r.full_name,r.enabled FROM review_pull_requests p '
        'JOIN repositories r ON r.id=p.repository_id WHERE p.unit_id=?', (unit['id'],)).fetchone()
    candidate = db.execute('SELECT * FROM candidates WHERE id=? AND unit_id=?',
        (unit['current_candidate_id'], unit['id'])).fetchone()
    source = db.execute("SELECT json_extract(source_json,'$.title') AS title,"
        "json_extract(source_json,'$.author.login') AS author FROM review_candidate_sources WHERE candidate_id=? AND unit_id=?",
        (unit['current_candidate_id'], unit['id'])).fetchone()
    gates = applicable_gates(db, unit, candidate_id=unit['current_candidate_id'])
    active = db.execute('SELECT j.id,j.phase FROM jobs j JOIN job_leases l ON l.job_id=j.id '
        'WHERE j.unit_id=? AND j.candidate_id=? AND j.cleanup=0 AND j.fenced=0 AND j.outcome IS NULL '
        'ORDER BY j.created_at DESC,j.id DESC LIMIT 1', (unit['id'], unit['current_candidate_id'])).fetchone()
    terminal = unit['state'] in ('closed', 'merged', 'dissolved')
    passed = db.execute("SELECT 1 FROM external_effects e JOIN candidate_effects c ON c.effect_id=e.id "
        "WHERE c.candidate_id=? AND c.fencing_generation=? AND c.membership_generation=? "
        "AND e.kind='github_check' AND e.status='confirmed' AND json_extract(e.payload_json,'$.review_only')=1 "
        "AND json_extract(e.payload_json,'$.conclusion')='success' LIMIT 1",
        (unit['current_candidate_id'], unit['fencing_generation'], unit['membership_generation'])).fetchone()
    status, reason_code, retry_at = 'queued', None, None
    reason = {'reviewing': 'Waiting for a reviewer.', 'auditing': 'Waiting for a security auditor.',
              'merging': 'Publishing the Review and Audit result.'}.get(unit['state'], describe(unit['state']))
    if terminal:
        status = 'completed'
        event = db.execute("SELECT payload_json FROM events WHERE unit_id=? AND kind='phase_changed' "
            'ORDER BY sequence DESC LIMIT 1', (unit['id'],)).fetchone()
        terminal_evidence = json.loads(event[0]) if event else {}
        reason = ('Merged on GitHub; remaining Forge work stopped.' if terminal_evidence.get('pull_merged') is True else
                  'Pull request closed; Forge work stopped.' if unit['close_code'] == 'pull_request_closed' else
                  describe(unit['close_code'] or unit['state']))
    elif gates:
        blocker = gates[0]; reason_code = blocker['code']; reason = _reason(blocker, now)
        details = json.loads(blocker['details_json'])
        deferred = reason_code == 'review_source' and details.get('reason') == 'review_unavailable'
        status = 'deferred' if deferred else 'blocked'
        retry_at = details.get('retry_at') if deferred else None
    elif active:
        status = 'running' if active['phase'] == 'running' else 'queued'
        reason = ('Reviewing this revision.' if unit['state'] == 'reviewing' else 'Auditing this revision.') if status == 'running' else 'Starting the worker.'
    elif passed:
        status, reason = 'completed', 'Forge Review and Audit passed for this revision.'
    number, repo = binding['pull_number'], binding['full_name']
    public = dict(key=f"pr:{binding['repository_id']}:{number}", repo_id=binding['repository_id'], repo=repo,
        number=number, title=source['title'] if source else f'Pull request #{number}', author=source['author'] if source else None,
        url=f'https://github.com/{repo}/pull/{number}', unit_id=unit['id'],
        candidate=dict(id=candidate['id'], head=candidate['head_oid'], base=candidate['base_oid'],
                       base_ref=unit['base_branch'], pr=number) if candidate else None,
        stage_code=unit['state'], state=unit['state'], status=status, reason=reason, reason_code=reason_code,
        retry_at=retry_at, active_job_id=active['id'] if active and not terminal else None,
        updated_at=unit['updated_at'], created_at=unit['created_at'], enabled=bool(binding['enabled']))
    return public, [dict(code=gate['code'], reason=_reason(gate, now)) for gate in gates[:50]]


def _units_in(db, states, limit, before=''):
    boundary = None
    if before:
        raw, separator, identifier = before.partition(':')
        try:
            at = float(raw)
        except ValueError:
            raise ValueError('Invalid pull request cursor') from None
        if not separator or not identifier or len(identifier)>100 or not math.isfinite(at):
            raise ValueError('Invalid pull request cursor')
        boundary = (at,identifier)
    units = []
    for state in states:
        clause = ' AND (u.updated_at,u.id)<(?,?)' if boundary else ''
        units += db.execute('SELECT u.* FROM units u INDEXED BY open_units '
            'CROSS JOIN review_pull_requests p ON p.unit_id=u.id WHERE u.state=? AND ' + LATEST + clause +
            ' ORDER BY u.updated_at DESC,u.id DESC LIMIT ?', (state,*(boundary or ()),limit+1)).fetchall()
    return sorted(units,key=lambda row:(row['updated_at'],row['id']),reverse=True)[:limit+1]


def listing_in(db, now, *, before='', limit=50):
    units = _units_in(db, ('reviewing','auditing','merging'), limit, before)
    return dict(pull_requests=[row_in(db,dict(unit),now)[0] for unit in units[:limit]],
                next=f"{units[limit-1]['updated_at']}:{units[limit-1]['id']}" if len(units)>limit else None)


def board_in(db, now, *, completed=False):
    active = dict(pull_requests=[],next=None) if completed else listing_in(db,now,limit=100)
    recent_limit = 50 if completed else 20
    recent = _units_in(db,('closed','merged'),recent_limit)
    return dict(pull_requests=active['pull_requests']+[row_in(db,dict(unit),now)[0] for unit in recent[:recent_limit]],
                pull_requests_next=active['next'],pull_requests_recent_limit=recent_limit,
                pull_requests_recent_truncated=len(recent)>recent_limit)


def jobs_in(db, repo_id, number, before):
    # Page bindings then their indexed jobs. Retargeted units remain visible,
    # without sorting/scanning a pull request's entire retained job history.
    boundary = None
    if before:
        parts = before.split(':')
        if len(parts) != 4:
            raise ValueError('Invalid pull request history cursor')
        try:
            boundary = (float(parts[0]), parts[1], float(parts[2]), parts[3])
        except ValueError:
            raise ValueError('Invalid pull request history cursor') from None
        if not all(math.isfinite(boundary[i]) for i in (0,2)) or not boundary[1] or len(boundary[1])>100 or len(boundary[3])>100:
            raise ValueError('Invalid pull request history cursor')
    params = [repo_id, number]; clause = ''
    if boundary:
        clause = ' AND (created_at,unit_id)' + ('<=' if boundary[3] else '<') + '(?,?)'
        params.extend(boundary[:2])
    units = db.execute('SELECT unit_id,created_at FROM review_pull_requests WHERE repository_id=? AND pull_number=?' +
        clause + ' ORDER BY created_at DESC,unit_id DESC LIMIT 21', params).fetchall()
    result = []
    for unit in units[:20]:
        params = [unit['unit_id']]; clause = ''
        if boundary and unit['unit_id'] == boundary[1]:
            clause = ' AND (created_at,id)<(?,?)'; params.extend(boundary[2:])
        rows = db.execute('SELECT id,created_at FROM jobs WHERE unit_id=?' + clause +
            ' ORDER BY created_at DESC,id DESC LIMIT ?', (*params,21-len(result))).fetchall()
        result.extend((row['id'], f"{unit['created_at']}:{unit['unit_id']}:{row['created_at']}:{row['id']}") for row in rows)
        if len(result)>20:
            return [row[0] for row in result[:20]], result[19][1]
    next_cursor = f"{units[19]['created_at']}:{units[19]['unit_id']}:0:" if len(units)>20 else None
    return [row[0] for row in result], next_cursor
