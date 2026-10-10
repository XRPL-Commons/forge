"""Version-one task evidence decoder; captured manifests remain byte-identical."""
import json


def save_legacy_tasks_in(db,unit,plan_id,builder_id,manifest,plan,clock):
    pins={pin['member_id']:pin['requirements_revision_id'] for pin in manifest['members']}
    keys={child['member_id']:'v1:'+plan_id+':'+child['key'] for child in plan['children']}
    for child in plan['children']:
        identity=keys[child['member_id']]
        db.execute('INSERT INTO delivery_tasks VALUES(?,?,?,?,?,?,?,?)',(identity,builder_id,plan_id,child['key'],'child',child['scope'],'[]',clock))
        db.execute('INSERT INTO delivery_task_members VALUES(?,?,?)',(identity,child['member_id'],pins[child['member_id']]))
        db.execute('INSERT INTO delegation_plan_tasks VALUES(?,?)',(plan_id,identity))
    from .delivery_inputs import current_output_in
    retained=set()
    def retain(identity):
        if identity in retained: return
        retained.add(identity)
        db.execute('INSERT OR IGNORE INTO delegation_plan_tasks VALUES(?,?)',(plan_id,identity))
        for row in db.execute('SELECT prerequisite_task_id FROM delivery_task_dependencies WHERE task_id=?',(identity,)).fetchall(): retain(row[0])
    for child in plan['children']:
        for predecessor in child['dependencies']:
            identity=keys.get(predecessor)
            if identity is None:
                identity=current_output_in(db,unit,predecessor,base_oid=manifest['base_oid'],policy_hash=manifest['policy_hash'])['task_id']
                retain(identity)
            db.execute('INSERT INTO delivery_task_dependencies VALUES(?,?)',(keys[child['member_id']],identity))
    for pin in manifest['members']:
        if pin['member_id'] not in keys:
            output=current_output_in(db,unit,pin['member_id'],base_oid=manifest['base_oid'],policy_hash=manifest['policy_hash'])
            if output: retain(output['task_id'])
