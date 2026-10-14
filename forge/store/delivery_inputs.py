"""Exact member/output identities shared by reservation and projection."""
import json


def approved_in(db, unit_id):
    return db.execute('SELECT 1 FROM unit_publication_targets WHERE unit_id=?',(unit_id,)).fetchone() is not None


def members_in(db, unit_id):
    return [dict(row) for row in db.execute('SELECT * FROM unit_issues WHERE unit_id=? AND left_at IS NULL ORDER BY id', (unit_id,))]


def dependencies_in(db, unit, member_id):
    return [row[0] for row in db.execute('SELECT prerequisite_member_id FROM member_dependencies '
             'WHERE unit_id=? AND member_id=? AND membership_generation=? ORDER BY prerequisite_member_id',
             (unit['id'], member_id, unit['membership_generation']))]


def output_ref(output):
    return {key: output[key] for key in ('member_id', 'commit_oid', 'input_manifest_hash')} | {'output_id': output['id']}


def current_output_in(db, unit, member_id, *, base_oid=None, policy_hash=None, visiting=()):
    if member_id in visiting:
        return None
    member = db.execute('SELECT * FROM unit_issues WHERE unit_id=? AND id=? AND left_at IS NULL', (unit['id'], member_id)).fetchone()
    if member is None:
        return None
    expected = []
    for prerequisite in dependencies_in(db, unit, member_id):
        output = current_output_in(db, unit, prerequisite, base_oid=base_oid, policy_hash=policy_hash,
                                   visiting=(*visiting, member_id))
        if output is None:
            return None
        expected.append(output_ref(output))
    for row in db.execute('SELECT p.*,a.input_manifest_json FROM part_outputs p JOIN attempts a ON a.id=p.attempt_id '
            'WHERE p.member_id=? AND p.requirements_revision_id=? '
            'AND NOT EXISTS(SELECT 1 FROM part_output_invalidations i WHERE i.output_id=p.id) '
            'ORDER BY p.accepted_at DESC,p.id DESC', (member_id, member['requirements_revision_id'])):
        manifest = json.loads(row['input_manifest_json'])
        if ((base_oid is None or row['base_oid'] == base_oid)
                and (policy_hash is None or manifest.get('policy_hash') == policy_hash)
                and manifest.get('prerequisite_outputs') == expected):
            return dict(row)
    return None


def member_dependency_problem_in(db, unit, member_id, *, contribution_cache=None):
    from .task_inputs import current_plan_in,member_contributions_in
    plan=current_plan_in(db,unit['id'])
    if plan and json.loads(plan['plan_json']).get('version')==2:
        from .task_inputs import contribution_collection_in
        cache={} if contribution_cache is None else contribution_cache
        if unit['id'] not in cache: cache[unit['id']]=contribution_collection_in(db,unit)
        return any(not cache[unit['id']][prerequisite]['ready'] for prerequisite in dependencies_in(db,unit,member_id))
    return any(current_output_in(db, unit, prerequisite) is None for prerequisite in dependencies_in(db, unit, member_id))


def member_pins_in(db, unit):
    return [{'member_id': row['id'], 'requirements_revision_id': row['requirements_revision_id']}
            for row in members_in(db, unit['id'])]


def requirements_hash_in(db, unit):
    from .transitions import manifest_hash
    return manifest_hash([{'member_id': row['id'], 'issue_id': row['issue_id'],
                           'requirements_revision_id': row['requirements_revision_id'],
                           'requirements_hash': db.execute('SELECT requirements_hash FROM issue_revisions WHERE id=?',
                               (row['requirements_revision_id'],)).fetchone()[0]}
                          for row in members_in(db, unit['id'])])


def existing_candidates_in(db, unit_id):
    """Exact source candidates and author identities from applied approval."""
    decision = db.execute("SELECT payload_json FROM decisions WHERE unit_id=? AND command='compose' AND status='applied'", (unit_id,)).fetchone()
    receipts = {row['candidate_id']: row for row in json.loads(decision[0])['existing_prs']} if decision else {}
    sources = []
    for row in db.execute('SELECT s.source_unit_id,c.id AS candidate_id,c.pull_number,c.branch,c.head_oid,c.base_oid,c.requirements_hash '
                          'FROM unit_composition_sources s JOIN candidates c ON c.id=s.source_candidate_id '
                          'WHERE s.unit_id=? AND s.source_candidate_id IS NOT NULL ORDER BY s.source_unit_id', (unit_id,)):
        source = dict(row)
        source['author'] = receipts[source['candidate_id']]['author']
        sources.append(source)
    return sources


def publication_author_in(db, unit_id, previous):
    """The author observed in a confirmed host publication for this exact head."""
    rows = db.execute("SELECT p.job_id,r.receipt_json FROM publication_receipts r "
        "JOIN publication_intents p ON p.id=r.intent_id WHERE p.unit_id=? AND p.status='confirmed' "
        "AND json_extract(r.receipt_json,'$.branch')=? AND json_extract(r.receipt_json,'$.head_oid')=? "
        "AND json_extract(r.receipt_json,'$.pull_number')=? ORDER BY r.observed_at DESC LIMIT 2",
        (unit_id, previous['branch'], previous['head_oid'], previous['pull_number'])).fetchall()
    authors = [json.loads(row['receipt_json']).get('pr_author') for row in rows
               if not previous.get('source_job_id') or row['job_id'] == previous['source_job_id']]
    if authors and authors[0] and all(author == authors[0] for author in authors):
        return {**previous, 'author': authors[0]}
    return previous


def previous_publication_in(db, unit_id):
    """Exact accepted, base-invalidated, or operator-reloaded publication."""
    event = db.execute("SELECT e.kind,e.job_id,e.payload_json FROM events e "
        "WHERE e.unit_id=? AND (e.kind='candidate_selected' OR e.kind='attempt_fenced' "
        "AND json_extract(e.payload_json,'$.reason')='base_changed' "
        "AND EXISTS(SELECT 1 FROM jobs j WHERE j.id=e.job_id AND j.role='build') "
        "OR e.kind='candidate_superseded' AND json_extract(e.payload_json,'$.reason')='candidate_changed' "
        "AND json_type(e.payload_json,'$.evidence.operator_request')='text' "
        "AND json_type(e.payload_json,'$.evidence.publication')='object') "
        "ORDER BY e.sequence DESC LIMIT 1", (unit_id,)).fetchone()
    if event is None:
        return None
    payload = json.loads(event['payload_json'])
    if event['kind'] == 'candidate_selected':
        return publication_author_in(db, unit_id, dict(db.execute('SELECT * FROM candidates WHERE id=?', (payload['candidate_id'],)).fetchone()))
    if event['kind']=='candidate_superseded':
        return publication_author_in(db, unit_id, {**payload['evidence']['publication'],'source_job_id':payload['evidence']['source_job_id']})
    return publication_author_in(db, unit_id, {**payload['publication'], 'source_job_id': event['job_id']})


from .task_inputs import member_contributions_in
