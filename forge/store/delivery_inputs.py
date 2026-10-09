"""Exact member/output identities shared by reservation and projection."""
import json


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


def member_dependency_problem_in(db, unit, member_id):
    return any(current_output_in(db, unit, prerequisite) is None
               for prerequisite in dependencies_in(db, unit, member_id))


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
