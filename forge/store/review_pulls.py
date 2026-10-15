"""Human pull requests share the candidate lifecycle, without issue membership."""
import json
import re
import time
import uuid

from .models import Command, TransitionError
from .runtime import writable
from .transitions import Lifecycle, _id, _json, _unit, applicable_gates, manifest_hash

SHA = re.compile(r'[0-9a-f]{40}')
REPO = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')


def normalize_source(remote, repo_id, repo):
    """Freeze a complete authenticated GitHub PR observation, including forks."""
    if not isinstance(remote, dict) or type(repo_id) is not int or repo_id <= 0 or not isinstance(repo, str) or not REPO.fullmatch(repo):
        raise TransitionError('review PR requires an exact repository identity')
    number = remote.get('number')
    author = remote.get('user')
    if (type(number) is not int or number <= 0 or not isinstance(author, dict)
            or type(author.get('id')) is not int or author['id'] <= 0
            or not isinstance(author.get('login'), str) or not author['login']
            or author.get('type') not in {'User', 'Bot'}):
        raise TransitionError('review PR author or number is unverified')
    refs = {}
    for name in ('head', 'base'):
        ref = remote.get(name) or {}
        repository = ref.get('repo') or {}
        if (not isinstance(ref.get('sha'), str) or not SHA.fullmatch(ref['sha'])
                or not isinstance(ref.get('ref'), str) or not ref['ref']
                or type(repository.get('id')) is not int or repository['id'] <= 0
                or not isinstance(repository.get('full_name'), str) or not REPO.fullmatch(repository['full_name'])):
            raise TransitionError('review PR commit or repository identity is unverified')
        refs[name] = {'sha': ref['sha'], 'ref': ref['ref'],
                      'repo': {key: repository[key] for key in ('id', 'full_name')}}
    if refs['base']['repo'] != {'id': repo_id, 'full_name': repo}:
        raise TransitionError('review PR belongs to another repository')
    if remote.get('state') not in {'open', 'closed'} or type(remote.get('draft')) is not bool or type(remote.get('merged', False)) is not bool:
        raise TransitionError('review PR lifecycle is unverified')
    title, body = remote.get('title'), remote.get('body')
    if not isinstance(title, str) or body is not None and not isinstance(body, str):
        raise TransitionError('review PR intent is unverified')
    labels = remote.get('labels', [])
    if not isinstance(labels, list) or any(not isinstance(label, (str, dict)) for label in labels):
        raise TransitionError('review PR labels are unverified')
    labels = [label.get('name') if isinstance(label, dict) else label for label in labels]
    if any(not isinstance(label, str) for label in labels):
        raise TransitionError('review PR labels are unverified')
    source = dict(version=1, repo_id=repo_id, repo=repo, pull_number=number, title=title, body=body or '',
                  author={key: author[key] for key in ('id', 'login', 'type')}, **refs,
                  state=remote['state'], draft=remote['draft'], labels=sorted(set(labels)), merged=remote.get('merged', False))
    if len(_json(source).encode()) > 1000000:
        raise TransitionError('review PR source exceeds its complete observation bound')
    return source


def exact_source_matches(remote, source):
    try:
        return normalize_source(remote, source['repo_id'], source['repo']) == source
    except (TransitionError, KeyError, TypeError):
        return False


def review_only_in(db, unit_id):
    row = db.execute('SELECT * FROM review_pull_requests WHERE unit_id=?', (unit_id,)).fetchone()
    return dict(row) if row else None


def source_in(db, candidate_id):
    row = db.execute('SELECT source_json FROM review_candidate_sources WHERE candidate_id=?', (candidate_id,)).fetchone()
    return json.loads(row[0]) if row else None


def snapshot_in(db, unit):
    binding = review_only_in(db, unit['id'])
    if not binding:
        return None
    row = db.execute('SELECT * FROM candidates WHERE id=?', (unit['current_candidate_id'],)).fetchone()
    candidate = dict(row) if row else None
    source = source_in(db, unit['current_candidate_id'])
    role = {'reviewing': 'review', 'auditing': 'audit'}.get(unit['state'])
    gates = applicable_gates(db, unit, candidate_id=unit['current_candidate_id'])
    reason = gates[0]['code'] if gates else None
    if db.execute('SELECT 1 FROM jobs WHERE unit_id=? AND cleanup=0', (unit['id'],)).fetchone():
        reason = reason or 'cleanup_pending'
    if role is None or source is None:
        reason = reason or 'phase_ineligible'
    memory = {row['role']: row['memory_bytes'] for row in db.execute(
        'SELECT role,memory_bytes FROM memory_profiles WHERE repository_id=? ORDER BY revision', (unit['repository_id'],))}
    return dict(unit=unit, review_only=True, review_source=source, repo=source['repo'] if source else None,
                members=[], member=None, issue=None, grouped=False, candidate=candidate, role=role, reason=reason,
                environment={}, memory_bytes=memory, resume_branch=None, resume_candidate=None,
                reviewers_requested=None, steward_retry=None)


def invalidate_in(lifecycle, db, command, unit, reason, evidence):
    """A rejected or superseded human candidate waits for its author, never a builder."""
    lifecycle._fence(db, command)
    lifecycle._open_gate(db, command, 'review_source',
        {'source': 'review_pull', 'reason': reason, 'candidate_id': unit['current_candidate_id'], 'evidence': evidence})
    lifecycle._event(db, command, 'candidate_superseded',
        {'candidate_id': unit['current_candidate_id'], 'reason': reason, 'evidence': evidence})
    if reason in {'technical_rejection', 'verdict_failed'} and unit['current_candidate_id']:
        from .effects import Effects
        fresh = _unit(db, unit['id'])
        candidate = dict(db.execute('SELECT * FROM candidates WHERE id=?', (unit['current_candidate_id'],)).fetchone())
        Effects(lifecycle.store, clock=lifecycle.clock)._insert(db, command, fresh, candidate, 'github_check',
            dict(candidate_id=candidate['id'], role='controller', conclusion='failure',
                 summary='Forge Review or Audit requested changes on this pull request revision.'))


class ReviewPullRequests(Lifecycle):
    def __init__(self, store, *, clock=time.time):
        super().__init__(store, clock=clock)

    def _retire_in(self, db, unit, *, reason):
        command = Command(str(uuid.uuid4()), 'supervisor:review-pulls', unit['id'], unit['version'])
        self._fence(db, command)
        for gate in db.execute('SELECT id FROM unit_gates WHERE unit_id=? AND resolved_at IS NULL', (unit['id'],)).fetchall():
            self._resolve(db, command, gate['id'], {'review_pull_retired': reason}, resolution='superseded')
        db.execute("UPDATE units SET state='closed',close_code=?,primary_gate_id=NULL WHERE id=?", (reason, unit['id']))
        self._event(db, command, 'phase_changed', {'before': unit['state'], 'after': 'closed', 'reason': reason})
        return self._save(db, command, 'retire_review_pull', {'reason': reason}, {'admitted': False, 'reason': reason})

    def observe(self, source, *, policy_hash, scope='all', enabled=True, hold_labels=('hold', 'blocked'), expected_version=None):
        """Admit/refresh one complete PR observation. Performs no remote writes."""
        if scope not in {'all', 'forge'} or type(enabled) is not bool:
            raise TransitionError('review PR scope is invalid')
        # Revalidate callers' frozen dictionaries through the same exact shape.
        remote = {**source, 'number': source.get('pull_number'), 'user': source.get('author')}
        if normalize_source(remote, source.get('repo_id'), source.get('repo')) != source:
            raise TransitionError('review PR source is not canonical')
        if not isinstance(policy_hash, str) or not policy_hash:
            raise TransitionError('review PR policy is required')
        blocked = ('scope_excluded' if scope != 'all' else 'runtime_disabled' if not enabled
                   else 'pull_closed' if source['state'] != 'open' or source['merged']
                   else 'pull_draft' if source['draft'] else 'label_hold' if set(source['labels']) & set(hold_labels) else None)
        payload = dict(source_hash=manifest_hash(source), policy_hash=policy_hash, blocked=blocked)
        with self.store.write() as db:
            writable(db)
            repository = db.execute('SELECT * FROM repositories WHERE id=?', (source['repo_id'],)).fetchone()
            if not repository or repository['full_name'] != source['repo']:
                raise TransitionError('review PR repository is not configured')
            if not repository['enabled']:
                blocked = payload['blocked'] = 'repository_disabled'
            existing = db.execute("SELECT u.* FROM review_pull_requests p JOIN units u ON u.id=p.unit_id "
                "WHERE p.repository_id=? AND p.pull_number=? AND u.state NOT IN ('merged','closed') "
                'ORDER BY p.created_at DESC,u.id LIMIT 1', (source['repo_id'], source['pull_number'])).fetchone()
            if expected_version is not None and (existing['version'] if existing else 0) != expected_version:
                return {'admitted': False, 'reason': 'observation_changed'}
            if not existing and blocked:
                return {'admitted': False, 'reason': blocked}
            carry = []
            if existing and (source['state'] == 'closed' or source['merged']):
                return self._retire_in(db, dict(existing), reason='pull_request_closed')
            if existing and existing['base_branch'] != source['base']['ref']:
                # A new target branch is a new unit identity. Carry independent
                # unit holds to the successor; old worker/receipt histories stay.
                for gate in db.execute('SELECT * FROM unit_gates WHERE unit_id=? AND resolved_at IS NULL '
                        'AND candidate_id IS NULL AND member_id IS NULL AND job_id IS NULL', (existing['id'],)):
                    details = json.loads(gate['details_json'])
                    if not str(details.get('source', '')).startswith('review_pull') and gate['code'] not in {'cleanup_pending','result_pending','recovery_pending'}:
                        carry.append((gate['code'], details))
                self._retire_in(db, dict(existing), reason='pull_retargeted')
                existing = None
            initial = existing is None
            unit_id = str(uuid.uuid4()) if initial else existing['id']
            request = str(uuid.uuid4())
            command = Command(request, 'supervisor:review-pulls', unit_id, 1 if initial else existing['version'])
            if initial:
                db.execute("INSERT INTO units(id,repository_id,base_branch,state,created_at,updated_at) VALUES(?,?,?,'reviewing',?,?)",
                           (unit_id, source['repo_id'], source['base']['ref'], self.clock(), self.clock()))
                db.execute('INSERT INTO review_pull_requests VALUES(?,?,?,?)',
                           (unit_id, source['repo_id'], source['pull_number'], self.clock()))
                for code, details in carry:
                    self._open_gate(db, command, code, details)
            unit = _unit(db, unit_id)
            current_source = source_in(db, unit['current_candidate_id'])
            current = db.execute('SELECT * FROM candidates WHERE id=?', (unit['current_candidate_id'],)).fetchone()
            own_gates = db.execute("SELECT * FROM unit_gates WHERE unit_id=? AND code='review_source' AND resolved_at IS NULL "
                                  "AND json_extract(details_json,'$.source')='review_pull'", (unit_id,)).fetchall()
            if blocked:
                if any(json.loads(g['details_json']).get('reason') == blocked for g in own_gates):
                    return {'unit_id': unit_id, 'admitted': False, 'reason': blocked}
                invalidate_in(self, db, command, unit, blocked, {'source_hash': payload['source_hash']})
                return self._save(db, command, 'observe_review_pull', payload, {'admitted': False, 'reason': blocked}, initial=initial)
            unchanged = current_source == source and current is not None and current['policy_hash'] == policy_hash
            # A rejection is released by a new source, not by the next poll.
            admission_reasons = {'scope_excluded', 'runtime_disabled', 'repository_disabled', 'pull_closed', 'pull_draft', 'label_hold'}
            release = [g for g in own_gates if not unchanged or json.loads(g['details_json']).get('reason') in admission_reasons]
            policy_wait = db.execute("SELECT g.id,e.status,c.fencing_generation,c.membership_generation FROM unit_gates g "
                "JOIN external_effects e ON e.id=json_extract(g.details_json,'$.effect_id') "
                "JOIN candidate_effects c ON c.effect_id=e.id WHERE g.unit_id=? AND g.candidate_id=? "
                "AND g.resolved_at IS NULL AND json_extract(g.details_json,'$.source')='review_pull_policy'",
                (unit_id, unit['current_candidate_id'])).fetchone()
            repair_policy = bool(unchanged and policy_wait and (policy_wait['status'] == 'superseded'
                or policy_wait['fencing_generation'] != unit['fencing_generation']
                or policy_wait['membership_generation'] != unit['membership_generation']))
            if unchanged and not release and not repair_policy:
                return {'unit_id': unit_id, 'candidate_id': current['id'], 'admitted': True, 'changed': False}
            for gate in release:
                self._resolve(db, command, gate['id'], {'source_hash': payload['source_hash']}, resolution='superseded')
            if not unchanged:
                if not initial:
                    self._fence(db, command)
                    self._event(db, command, 'candidate_superseded', {'candidate_id': unit['current_candidate_id'], 'reason': 'pull_source_changed'})
                candidate_id = _id(request, 'candidate')
                requirements_hash = manifest_hash({key: source[key] for key in ('title', 'body')})
                input_hash = manifest_hash(dict(source=source, policy_hash=policy_hash))
                db.execute('INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (candidate_id, unit_id, source['pull_number'], source['head']['ref'], source['head']['sha'], source['base']['sha'],
                     policy_hash, requirements_hash, unit['membership_generation'], input_hash, self.clock()))
                db.execute('INSERT INTO review_candidate_sources VALUES(?,?,?,?)',
                           (candidate_id, unit_id, payload['source_hash'], _json(source)))
                db.execute("UPDATE units SET state='reviewing',base_branch=?,current_candidate_id=? WHERE id=?",
                           (source['base']['ref'], candidate_id, unit_id))
                self._event(db, command, 'candidate_selected', {'candidate_id': candidate_id, 'source': 'review_pull'})
            else:
                candidate_id = current['id']
            if not unchanged or repair_policy:
                # Replace any prior green check on a reused head before Review.
                # Fencing also needs a fresh intent after an operator pause.
                from .effects import Effects
                fresh = _unit(db, unit_id)
                identity = dict(db.execute('SELECT * FROM candidates WHERE id=?', (candidate_id,)).fetchone())
                effect = Effects(self.store, clock=self.clock)._insert(db, command, fresh, identity, 'github_check',
                    dict(candidate_id=candidate_id, role='controller', conclusion=None,
                         summary='Forge Review and Audit are required for this exact pull request revision.'))
                for gate in db.execute("SELECT id FROM unit_gates WHERE unit_id=? AND resolved_at IS NULL "
                        "AND json_extract(details_json,'$.source')='review_pull_policy'", (unit_id,)).fetchall():
                    self._resolve(db, command, gate['id'], {'candidate_changed': candidate_id}, resolution='superseded')
                self._open_gate(db, command, 'observation_unavailable',
                    {'source': 'review_pull_policy', 'effect_id': effect['effect_id']}, candidate_id=candidate_id)
            return self._save(db, command, 'observe_review_pull', payload,
                              {'candidate_id': candidate_id, 'admitted': True, 'changed': not unchanged})
