"""Explicit prerequisite references and their GitHub completion semantics."""
import json
from .contracts import dependency_references


def references(body, repository):
    return dependency_references(body, repository)


def blocker(fact):
    if fact.get('error'):
        return fact['error']
    if fact['kind'] == 'internal':
        return None if fact['ready'] else 'prerequisite_output_pending'
    if fact['state'] != 'closed':
        return 'prerequisite_open'
    if fact['kind'] == 'pull_request':
        return None if fact['merged'] else 'prerequisite_closed_unmerged'
    return { 'completed': None, 'duplicate': 'prerequisite_duplicate',
             'not_planned': 'prerequisite_not_planned' }.get(fact['state_reason'], 'prerequisite_completion_unknown')


_IDENTITY = ('repository_id', 'github_id', 'number', 'kind')


def identity_blocker(fact, binding, *, repository_id, number, require_binding=False):
    """Completion cannot rebind a name or depend on the issue itself after a rename."""
    if fact.get('error'):
        return fact['error']
    if (fact.get('repository_id'), fact.get('number')) == (repository_id, number):
        return 'prerequisite_cycle'
    if binding is None and require_binding:
        return 'prerequisite_identity_unobserved'
    if binding and any(fact.get(key) != binding[key] for key in _IDENTITY):
        return 'prerequisite_identity_changed'
    return None


def collect(store, github, *, unit_id, member_id, requirements_revision_id, body,
            require_bindings=False):
    """Read one member's complete prerequisite scope, then fetch current evidence.

    This function never changes identities, gates, counters or unit versions. The
    requirements controller commits its observations; merge admission only reads.
    """
    from ..store.delivery_inputs import dependencies_in, member_contributions_in
    from ..store.models import TransitionError
    from ..store.repository_requirements import orders_in

    with store.read_snapshot() as db:
        unit = dict(db.execute('SELECT * FROM units WHERE id=?', (unit_id,)).fetchone())
        member = db.execute('SELECT m.*,i.github_number,r.full_name,v.requirements_json '
            'FROM unit_issues m JOIN issues i ON i.id=m.issue_id '
            'JOIN repositories r ON r.id=i.repository_id '
            'JOIN issue_revisions v ON v.id=m.requirements_revision_id '
            'WHERE m.unit_id=? AND m.id=? AND m.left_at IS NULL', (unit_id, member_id)).fetchone()
        if member is None or member['requirements_revision_id'] != requirements_revision_id:
            raise TransitionError('Prerequisite scope no longer matches the approved member revision')
        refs = set(references(body, member['full_name']))
        refs.update(orders_in(db, member['issue_id'], requirements_revision_id))
        refs.update(row[0] for row in db.execute(
            "SELECT lower(r.full_name)||'#'||i.github_number FROM issue_dependencies d "
            'JOIN issues i ON i.id=d.prerequisite_issue_id JOIN repositories r ON r.id=i.repository_id '
            'WHERE d.issue_id=? AND d.requirements_revision_id=?',
            (member['issue_id'], requirements_revision_id)))
        requirements = json.loads(member['requirements_json'])
        for gate in db.execute("SELECT id,details_json,resolved_at FROM unit_gates WHERE unit_id=? "
                "AND code='dependency' AND (member_id=? OR EXISTS(SELECT 1 FROM job_members p WHERE p.job_id=unit_gates.job_id AND p.member_id=?))", (unit_id, member_id,member_id)):
            details = json.loads(gate['details_json'])
            if details.get('requirements') != requirements and details.get('requirements_by_member',{}).get(member_id)!=requirements_revision_id:
                continue
            if gate['resolved_at'] is not None:
                event = db.execute("SELECT payload_json FROM events WHERE unit_id=? AND kind='gate_resolved' "
                    "AND json_extract(payload_json,'$.gate_id')=? ORDER BY sequence DESC LIMIT 1",
                    (unit_id, gate['id'])).fetchone()
                evidence = json.loads(event[0]).get('evidence', {}) if event else {}
                # A verified completion stays a dependency if it reopens. An
                # explicit operator dismissal stops the worker-declared wait.
                if evidence.get('verified') is not True or evidence.get('requirements_revision_id') != requirements_revision_id and evidence.get('verified_requirements',{}).get(member_id)!=requirements_revision_id:
                    continue
            refs.update(member['full_name'].lower()+'#'+str(n) for n in details.get('issues', []))
            refs.update(details.get('references', []))
        members = [dict(row) for row in db.execute(
            "SELECT m.id,i.github_number,lower(r.full_name)||'#'||i.github_number AS reference "
            'FROM unit_issues m JOIN issues i ON i.id=m.issue_id JOIN repositories r ON r.id=i.repository_id '
            'WHERE m.unit_id=? AND m.left_at IS NULL AND m.id!=?', (unit_id, member_id))]
        internal = {row['reference']: row['id'] for row in members}
        internal_identity = {(unit['repository_id'], row['github_number']): row['id'] for row in members}
        approved = set(dependencies_in(db, unit, member_id))
        # Point lookups avoid SQL variable-count limits for large collections.
        bindings = {}
        for ref in refs:
            row = db.execute('SELECT * FROM prerequisite_identities WHERE reference=?', (ref,)).fetchone()
            if row:
                bindings[ref] = dict(row)
        needed = {internal[ref] for ref in refs if ref in internal}
        needed.update(internal_identity[(binding['repository_id'], binding['number'])]
            for binding in bindings.values() if (binding['repository_id'], binding['number']) in internal_identity)
        contributions = {key: member_contributions_in(db, unit, key) for key in needed}

    facts, blocked, external_blocked = [], [], []
    for ref in sorted(refs):
        binding = bindings.get(ref)
        internal_member = internal.get(ref)
        if not internal_member and binding:
            internal_member = internal_identity.get((binding['repository_id'], binding['number']))
        if internal_member:
            if internal_member not in approved:
                raise TransitionError('Requirements declare a member dependency absent from the approved composition')
            contribution = contributions[internal_member]
            fact = dict(reference=ref, kind='internal', member_id=internal_member,
                        plan_id=contribution['plan_id'], candidate_id=contribution['candidate_id'],
                        output_ids=sorted(output['output_id'] for output in contribution['outputs']),
                        ready=contribution['ready'], owner_final_pending=contribution['owner_final_pending'])
            reason = blocker(fact)
        else:
            fact = github.prerequisite(ref, binding)
            if fact.get('reference') != ref:
                raise TransitionError('Prerequisite observation identifies a different reference')
            reason = identity_blocker(fact, binding, repository_id=unit['repository_id'],
                number=member['github_number'], require_binding=require_bindings) or blocker(fact)
        facts.append(fact)
        if reason:
            blocked.append({'reference': ref, 'reason': reason})
            if fact.get('kind') != 'internal':
                external_blocked.append({'reference': ref, 'reason': reason})
    return {'facts': facts, 'blocked': blocked, 'external_blocked': external_blocked}


def ground_claims(store, github, claims):
    """Resolve typed identities using host GitHub observations, never worker quotes."""
    from .github import GitHubUnavailable
    facts=[]
    for claim in claims:
        if claim['type']!='wait_on_issue':
            continue
        with store.read_snapshot() as db:
            repository=db.execute('SELECT full_name FROM repositories WHERE id=?',(claim['repository_id'],)).fetchone()
        if repository is None:
            raise ValueError('Dependency repository is not configured')
        ref=repository[0].lower()+'#'+str(claim['issue_number'])
        with store.read_snapshot() as db:
            row=db.execute('SELECT * FROM prerequisite_identities WHERE reference=?',(ref,)).fetchone()
            binding=dict(row) if row else None
        fact=github.prerequisite(ref,binding)
        if (fact.get('error') or fact.get('reference')!=ref
                or fact.get('repository_id')!=claim['repository_id'] or fact.get('number')!=claim['issue_number']
                or binding and any(fact.get(key)!=binding[key] for key in _IDENTITY)):
            raise GitHubUnavailable('Typed dependency identity could not be verified')
        if fact.get('kind')!='issue' or fact.get('state')!='open' or type(fact.get('github_id')) is not int or fact['github_id']<=0:
            raise ValueError('Dependency claim must identify an existing open issue')
        facts.append(fact)
    return facts
