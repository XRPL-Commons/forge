"""Revision-bound prerequisite decisions accepted in the caller's transaction."""
import json
import re
import time

from .models import Command, IdempotencyConflict, TransitionError, VersionConflict
from .runtime import writable
from .transitions import Lifecycle, _id, _json, project_unit

_REFERENCE = re.compile(r'([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)\Z')


def reference(value):
    """One canonical wire format for local and cross-repository prerequisites."""
    match = _REFERENCE.fullmatch(value) if isinstance(value, str) else None
    if match is None or len(value) > 300:
        raise TransitionError('A prerequisite must be owner/repository#number')
    return match[1].lower() + '#' + str(int(match[2]))


def orders_in(db, issue_id, revision_id):
    row = db.execute('SELECT references_json FROM prerequisite_orders WHERE issue_id=? '
        'AND requirements_revision_id=? ORDER BY generation DESC LIMIT 1', (issue_id, revision_id)).fetchone()
    return json.loads(row[0]) if row else []


def _cycles(db, own, targets):
    graph = {}
    for row in db.execute('SELECT lower(r.full_name)||\'#\'||i.github_number AS reference,o.references_json '
            'FROM prerequisite_orders o JOIN issues i ON i.id=o.issue_id '
            'JOIN repositories r ON r.id=i.repository_id WHERE o.requirements_revision_id=i.current_revision_id '
            'AND o.generation=(SELECT max(p.generation) FROM prerequisite_orders p '
            'WHERE p.issue_id=o.issue_id AND p.requirements_revision_id=o.requirements_revision_id)'):
        graph[row['reference']] = json.loads(row['references_json'])
    graph[own] = targets
    visited = set()
    def visit(key, path):
        if key in path:
            raise TransitionError('Prerequisite ordering contains a cycle')
        if key in visited:
            return
        for target in graph.get(key, ()):
            visit(target, (*path, key))
        visited.add(key)
    visit(own, ())


def order_in(db, *, issue_id, requirements_revision_id, prerequisites, actor_id, request_id):
    """Save approved intent and fence affected work atomically with proposal acceptance.

    The observation controller resolves GitHub identity and completion outside this
    transaction. Until that read, the new order cannot authorize a launch or merge.
    """
    writable(db)
    if not request_id or not actor_id or not isinstance(prerequisites, list):
        raise TransitionError('Ordering requires an actor, request and a prerequisite list')
    targets = sorted({reference(value) for value in prerequisites})
    if len(_json(targets).encode()) > 32768:
        raise TransitionError('Prerequisite references exceed the 32 KiB record limit')
    payload = dict(issue_id=issue_id, requirements_revision_id=requirements_revision_id, prerequisites=targets)
    previous = db.execute('SELECT * FROM prerequisite_orders WHERE request_id=?', (request_id,)).fetchone()
    if previous:
        if (previous['actor_id'], previous['issue_id'], previous['requirements_revision_id'], previous['references_json']) != (
                actor_id, issue_id, requirements_revision_id, _json(targets)):
            raise IdempotencyConflict('Ordering request belongs to another decision')
        return {'order_id': previous['id'], 'generation': previous['generation'], **payload}
    issue = db.execute('SELECT i.*,lower(r.full_name)||\'#\'||i.github_number AS reference FROM issues i '
        'JOIN repositories r ON r.id=i.repository_id WHERE i.id=?', (issue_id,)).fetchone()
    if issue is None or issue['current_revision_id'] != requirements_revision_id:
        raise VersionConflict('Issue requirements changed before ordering approval')
    if issue['github_state'] != 'open':
        raise TransitionError('Only open issues can receive a prerequisite order')
    _cycles(db, issue['reference'], targets)
    now = time.time()
    generation = db.execute('SELECT COALESCE(max(generation),0)+1 FROM prerequisite_orders WHERE issue_id=?', (issue_id,)).fetchone()[0]
    order_id = _id(request_id, 'prerequisite-order')
    db.execute('INSERT INTO prerequisite_orders VALUES(?,?,?,?,?,?,?,?)',
        (order_id, request_id, actor_id, issue_id, requirements_revision_id, generation, _json(targets), now))
    member = db.execute('SELECT * FROM unit_issues WHERE issue_id=? AND left_at IS NULL', (issue_id,)).fetchone()
    result = {'order_id': order_id, 'generation': generation, **payload}
    if member:
        unit = db.execute('SELECT * FROM units WHERE id=?', (member['unit_id'],)).fetchone()
        command = Command(request_id, actor_id, unit['id'], unit['version'])
        lifecycle = Lifecycle(None, clock=lambda: now)
        before = lifecycle._lifecycle_summary(project_unit(db, unit['id']))
        lifecycle.fence_member_in(db, command, member['id'], reason='prerequisites_changed')
        for gate in db.execute("SELECT * FROM unit_gates WHERE unit_id=? AND member_id=? AND code='dependency' AND resolved_at IS NULL",
                               (unit['id'], member['id'])).fetchall():
            lifecycle._resolve(db, command, gate['id'], {'reobserve_order_id': order_id})
        lifecycle._open_gate(db, command, 'dependency', {'source': 'runtime_dependencies',
            'requirements_revision_id': requirements_revision_id, 'order_id': order_id,
            'reason': 'prerequisites_need_observation'}, member['id'])
        lifecycle._save(db, command, 'order_prerequisites', payload, result, before=before)
    else:
        db.execute("INSERT INTO events(transaction_id,issue_id,kind,at,actor_id,payload_json) VALUES(?,?,'decision_applied',?,?,?)",
            (request_id, issue_id, now, actor_id, _json({'command': 'order_prerequisites', **result})))
    return result
