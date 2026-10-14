"""Read-only steward inputs derived from the existing lifecycle authority."""
import json

from ..steward_contract import ACTIONS, MODES, digest, fields, require
from .models import TransitionError
from .transitions import _charges, _unit


def autonomy_in(db, repository_id):
    row = db.execute("SELECT revision,settings_json FROM configuration WHERE domain='steward' AND subject=? AND active=1",
                     (str(repository_id),)).fetchone()
    settings = json.loads(row['settings_json']) if row else {}
    policy = settings.get('autonomy', 'reversible')
    require(policy in {'manual', 'reversible'}, 'Invalid repository steward autonomy')
    return {'revision': row['revision'] if row else 0, 'policy': policy}



def move_observations(choice, observations):
    """Only facts authorizing this move matter; read time and unrelated issues do not."""
    base = observations.get('base_oid')
    require(isinstance(base,str) and 0 < len(base) <= 64, 'Invalid observed target commit')
    value = {'base_oid':base}
    if choice['action'] in {'wait_on','fold'}:
        parameters = choice['parameters']
        matches = [row for row in observations['issues']
                   if row['repository_id']==parameters['repository_id'] and row['issue_number']==parameters['issue_number']]
        require(len(matches)<=1, 'Ambiguous selected issue observation')
        target = matches[0] if matches else None
        if target:
            require(target['state'] in {'open','closed'} and isinstance(target['revision'],str), 'Invalid selected issue observation')
        value['target'] = ({key:target[key] for key in ('repository_id','issue_number','state','revision')}
                           if target else None)
    return value


def authority_in(db, unit):
    """Semantic pins omit controller bookkeeping and the steward's own cleanup."""
    current = _unit(db, unit['id'])
    members = [dict(row) for row in db.execute(
        'SELECT m.id AS member_id,m.requirements_revision_id,i.current_revision_id,i.version AS issue_version,'
        'i.github_state,m.state FROM unit_issues m JOIN issues i ON i.id=m.issue_id '
        'WHERE m.unit_id=? AND m.left_at IS NULL ORDER BY m.id', (current['id'],))]
    # Member state is execution bookkeeping; requirements/issue facts are authority.
    for member in members: member.pop('state')
    candidate = db.execute('SELECT id,head_oid,base_oid,policy_hash,requirements_hash,input_manifest_hash '
                           'FROM candidates WHERE id=?', (current['current_candidate_id'],)).fetchone()
    gates = []
    for row in db.execute('SELECT id,code,member_id,candidate_id,job_id,details_json FROM unit_gates '
                          'WHERE unit_id=? AND resolved_at IS NULL ORDER BY id', (current['id'],)):
        details = json.loads(row['details_json'])
        job_id = row['job_id'] or details.get('job_id')
        if row['code'] in {'cleanup_pending', 'result_pending'} and job_id and db.execute(
                "SELECT 1 FROM jobs WHERE id=? AND role='steward'", (job_id,)).fetchone():
            continue
        if row['code'] == 'decision_required' and details.get('source') == 'steward' and db.execute(
                'SELECT 1 FROM steward_decisions WHERE decision_id=? AND unit_id=?',
                (details.get('decision_id'), current['id'])).fetchone():
            continue
        gates.append({**{key: row[key] for key in ('id','code','member_id','candidate_id','job_id')},
                      'digest': digest(json.loads(row['details_json']))})
    rules = db.execute('SELECT rules_revision FROM repository_planning WHERE repository_id=?',
                       (current['repository_id'],)).fetchone()
    # Configuration contents stay on the host; their revision/digest detects changed authority.
    configuration = [dict(row) for row in db.execute(
        "SELECT domain,subject,revision FROM configuration WHERE active=1 AND domain IN ('runtime.json','factory.json') ORDER BY domain,subject")]
    return {**{key: current[key] for key in ('membership_generation','fencing_generation','budget_generation')},
            'members': members, 'candidate': dict(candidate) if candidate else None, 'gates': gates,
            'repository_rules_revision': rules[0] if rules else 0, 'configuration': configuration,
            'autonomy': autonomy_in(db, current['repository_id']),
            'observation_changes': [dict(row) for row in db.execute(
                'SELECT c.previous_digest,c.observed_digest FROM steward_observation_changes c '
                'JOIN steward_decisions d ON d.decision_id=c.decision_id JOIN steward_turns t ON t.job_id=d.job_id '
                'WHERE c.unit_id=? ORDER BY t.cycle', (current['id'],))]}


def trigger_in(db, unit, mode, trigger):
    require(mode in MODES, 'Unsupported steward mode')
    if mode == 'recovery':
        fields(trigger, ('failed_job_id',))
        row = db.execute('SELECT j.id,j.attempt_id,j.cleanup,j.role,f.recovery_required FROM jobs j '
            'JOIN attempt_failures f ON f.attempt_id=j.attempt_id WHERE j.id=? AND j.unit_id=?',
            (trigger['failed_job_id'], unit['id'])).fetchone()
        require(row and row['cleanup'] and row['recovery_required'] and row['role'] != 'steward',
                'Recovery requires a cleaned failed assignment')
        return 'failure:' + row['id']
    if mode == 'stall':
        fields(trigger, ('gate_id',))
        row = db.execute("SELECT details_json FROM unit_gates WHERE id=? AND unit_id=? AND code='forge_problem' AND resolved_at IS NULL",
                         (trigger['gate_id'], unit['id'])).fetchone()
        details = json.loads(row[0]) if row else {}
        require(details.get('source') == 'scheduling' and details.get('reason') == 'stalled',
                'Stall mode requires the current scheduling-owned stall')
        return 'stall:' + trigger['gate_id']
    fields(trigger, ('decision_id', 'choice_id', 'choice_digest'))
    row = db.execute('SELECT s.choice_id,s.choice_digest FROM steward_selections s JOIN steward_decisions d '
        'ON d.decision_id=s.decision_id JOIN steward_turns t ON t.job_id=d.job_id WHERE s.decision_id=? AND t.unit_id=?',
        (trigger['decision_id'], unit['id'])).fetchone()
    require(row and row['choice_id'] == trigger['choice_id'] and row['choice_digest'] == trigger['choice_digest'],
            'Brief requires the exact accepted choice')
    return 'brief:' + trigger['decision_id']


def build_dossier_in(db, unit, *, mode, trigger, now, observations=None, config=None):
    unit = _unit(db, unit['id'])
    require(unit['state'] not in {'closed','merged','dissolved'}, 'Steward requires live work')
    trigger_key = trigger_in(db, unit, mode, trigger)
    authority = authority_in(db, unit)
    issue_refs = [dict(row) for row in db.execute('SELECT m.id AS member_id,m.issue_id,m.requirements_revision_id,'
        'r.content_hash,r.requirements_hash FROM unit_issues m JOIN issue_revisions r ON r.id=m.requirements_revision_id '
        'WHERE m.unit_id=? AND m.left_at IS NULL ORDER BY m.id', (unit['id'],))]
    artifacts = [dict(row) for row in db.execute('SELECT a.job_id,a.kind,a.sequence,a.digest FROM job_artifacts a '
        'JOIN jobs j ON j.id=a.job_id WHERE j.unit_id=? AND a.expired_at IS NULL '
        "AND a.kind IN ('failure_evidence','worker_capture','result','summary','steward_decision') "
        'ORDER BY a.created_at DESC,a.job_id,a.kind,a.sequence LIMIT 101', (unit['id'],))]
    notes = [dict(row) for row in db.execute('SELECT n.note_id,n.revision,n.kind FROM repository_note_revisions n '
        'WHERE n.repository_id=? AND n.deleted=0 AND n.revision=(SELECT max(x.revision) '
        'FROM repository_note_revisions x WHERE x.note_id=n.note_id) ORDER BY n.note_id LIMIT 51', (unit['repository_id'],))]
    repository = db.execute('SELECT full_name FROM repositories WHERE id=?', (unit['repository_id'],)).fetchone()
    count = db.execute('SELECT count(DISTINCT cycle) FROM steward_turns WHERE unit_id=?', (unit['id'],)).fetchone()[0]
    observations = observations or {'base_oid': None, 'observed_at': now, 'issues': []}
    fields(observations, ('base_oid', 'observed_at', 'issues'))
    require(isinstance(observations['issues'], list) and len(observations['issues']) <= 100, 'Too many related issue observations')
    for issue in observations['issues']:
        fields(issue, ('repository_id', 'issue_number', 'state', 'revision'))
        require(type(issue['repository_id']) is int and issue['repository_id'] > 0
                and type(issue['issue_number']) is int and issue['issue_number'] > 0
                and issue['state'] in {'open','closed'} and isinstance(issue['revision'], str), 'Invalid issue observation')
    require(observations['base_oid'] is None or isinstance(observations['base_oid'], str)
            and 0 < len(observations['base_oid']) <= 64, 'Invalid observed base')
    require(type(observations['observed_at']) in (int,float) and 0 <= observations['observed_at'] <= now,
            'Invalid observation time')
    config = config or {}
    capabilities = {'provider_ids': sorted(entry['id'] for entry in config.get('provider_pool', [])),
                    'environment_ids': sorted(config.get('environment_tiers', {})),
                    'timeout_seconds': dict(config.get('timeout_seconds', {})),
                    'timeout_bounds': {'min':30,'max':3000}}
    facts = {'issue_revision_refs': issue_refs, 'artifact_refs': artifacts[:100], 'note_revision_refs': notes[:50],
             'observations': observations, 'capabilities': capabilities,
             'forge_problem_signatures': [r[0] for r in db.execute('SELECT DISTINCT r.signature FROM forge_problem_report_jobs r JOIN jobs j ON j.id=r.job_id WHERE j.unit_id=? ORDER BY r.signature', (unit['id'],))],
             'charged_failures': _charges(db, unit), 'remaining_failure_budget': max(0, 3-_charges(db, unit)),
             'remaining_steward_cycles': max(0, 3-count)}
    value = {'version': 1, 'mode': mode, 'trigger': trigger, 'trigger_key': trigger_key,
             'repository': {'id': unit['repository_id'], 'name': repository[0], 'base_branch': unit['base_branch']},
             'scope': {'unit_id': unit['id'], 'member_ids': [m['member_id'] for m in authority['members']]},
             'authority': authority, 'facts': facts, 'allowed_moves': sorted(ACTIONS),
             'input_completeness': {'complete': len(artifacts)<=100 and len(notes)<=50}, 'created_at': now}
    value['id'] = digest(value)
    value['digest'] = digest(value)
    return value
