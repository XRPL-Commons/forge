"""Fresh merge authorization and durable intent for SQLite delivery candidates."""
import json
import time
import uuid

from ..store.db import StoreError
from ..store.effects import Effects
from ..store.models import Command, TransitionError
from ..store.queries import Queries
from ..store.transitions import Lifecycle, manifest_hash
from .config import policy
from .contracts import digest
from .github import GitHubUnavailable
from .ownership import ownership
from .verification import ci_result


def candidate_snapshot(store, unit_id):
    """One bounded candidate projection for GitHub adapters; no artifact reads."""
    with store.read_snapshot() as db:
        row = db.execute('SELECT u.*,r.full_name AS repo,r.id AS repo_id FROM units u '
                         'JOIN repositories r ON r.id=u.repository_id WHERE u.id=?', (unit_id,)).fetchone()
        if row is None:
            raise KeyError(unit_id)
        unit = dict(row)
        if unit['current_candidate_id'] is None:
            return None
        from ..store.candidate_scope import scope_in
        scope=scope_in(db,unit)
        identity = dict(db.execute('SELECT * FROM candidates WHERE id=?', (unit['current_candidate_id'],)).fetchone())
        from ..store.operator_views import reviewer_request_in
        value = dict(reviewers_requested=reviewer_request_in(db,identity['id']),unit_id=unit_id, unit_version=unit['version'], repo=unit['repo'], repo_id=unit['repo_id'],
            state=unit['state'], **scope,
            identity=identity, candidate_id=identity['id'], candidate=dict(key=identity['id'], pr=identity['pull_number'],
                branch=identity['branch'], head=identity['head_oid'], base=identity['base_oid'], base_ref=unit['base_branch'],
                accepted_at=identity['accepted_at']), evidence={}, gates=[], verified_checks=[])
        for role in ('review', 'audit'):
            evidence = db.execute('SELECT details_json FROM candidate_evidence WHERE candidate_id=? AND kind=? '
                                  'AND verdict=\'pass\' ORDER BY observed_at DESC,rowid DESC LIMIT 1',
                                  (identity['id'], role)).fetchone()
            if evidence:
                details = json.loads(evidence[0])
                value['evidence'][role] = {**details.get('report', details),
                    'check_id': details.get('check', {}).get('check_id', details.get('check_id'))}
        for gate in db.execute('SELECT * FROM unit_gates WHERE unit_id=? AND candidate_id=? ORDER BY created_at,id',
                               (unit_id, identity['id'])):
            entry = dict(gate)
            entry['details'] = json.loads(entry.pop('details_json'))
            value['gates'].append(entry)
        from .ownership import publication_context
        return publication_context(store,unit_id,value)


class MergeAdmission:
    """Rechecks current operator policy, issue ownership and exact GitHub evidence.

    config() and repository_ready(repo) must use the selected configuration and
    readiness authority. Neither callback may grant admission from worker output.
    """
    def __init__(self, store, github, *, config, repository_ready, clock=time.time):
        self.store, self.github = store, github
        self.config, self.repository_ready, self.clock = config, repository_ready, clock

    def evaluate_observation(self, effect):
        """Return authorization and display evidence without writing the store."""
        observation = None
        def result(authorization):
            return {'authorization': authorization, 'approval_observation': observation}

        item = candidate_snapshot(self.store, effect['unit_id'])
        if item is None or item['candidate_id'] != effect['candidate_id']:
            return result(None)
        config = self.config()
        if not isinstance(config, dict) or any(key not in config for key in ('human_required', 'human_reviewers', 'merge_method')):
            return result(None)
        identity = item['identity']
        effective_policy=policy(config,item['repo_id'],repository_name=item['repo'])
        if (not config.get('enabled') or not config.get('merge_enabled')
                or self.repository_ready(item['repo']) is not True
                or digest(effective_policy) != identity['policy_hash']):
            return result(None)
        if item['requirements_hash']!=identity['requirements_hash']:
            return result(None)
        pulls=self.github.pulls(item)
        for member in item['members']:
            scoped={**item,'number':member['number'],'requirements':member['requirements']}
            issue,requirements=self.github.issue(scoped)
            if issue.get('state')!='open' or requirements!=member['requirements']:
                return result(None)
            labels={label['name'] if isinstance(label,dict) else label for label in issue.get('labels',[])}
            if labels.intersection(config.get('hold_labels',['hold','blocked'])):
                return result(None)
            if ownership(issue,scoped,item['repo'],pulls) is not None:
                return result(None)
            from .prerequisites import collect
            prerequisites = collect(self.store, self.github, unit_id=item['unit_id'],
                member_id=member['member_id'], requirements_revision_id=member['requirements_revision_id'],
                body=issue.get('body'), require_bindings=True)
            if prerequisites['blocked']:
                return result(None)
        pr = self.github.pr(item, identity['pull_number'])
        if (pr.get('number') != identity['pull_number'] or pr.get('state') != 'open'
                or pr.get('draft') is not False or pr.get('merged') is not False or pr.get('mergeable') is not True
                or (pr.get('head') or {}).get('sha') != identity['head_oid']
                or (pr.get('base') or {}).get('sha') != identity['base_oid']
                or (pr.get('base') or {}).get('ref') != item['candidate']['base_ref']
                or (pr.get('head') or {}).get('repo', {}).get('id') != item['repo_id']
                or (pr.get('base') or {}).get('repo', {}).get('id') != item['repo_id']
                or self.github.target(item) != item['candidate']['base_ref']):
            return result(None)
        audit = item['evidence'].get('audit', {})
        if (audit.get('human_required') not in {'required', 'not_required'}
                or any(type(item['evidence'].get(role, {}).get('check_id')) is not int for role in ('review', 'audit'))):
            return result(None)
        required = effective_policy['human_required'] or audit['human_required'] == 'required'
        ci = {}
        for gate in item['gates']:
            if gate['code'] in {'verification_ci', 'verification_human'}:
                if gate['resolved_at'] is None:
                    return result(None)
                if gate['code'] == 'verification_human':
                    required = True
                    item['verified_checks'].append({'created_at': gate['resolved_at'], 'proof': {'via': 'human'}})
                for check in gate['details'].get('ci', []):
                    ci[(check['name'], check['app_id'])] = check
        if ci and not ci_result(self.github.ci_evidence(item), item['candidate'], list(ci.values())):
            return result(None)
        if not self.github.gates(item) or not self.github.protections(item, item['candidate']):
            return result(None)
        # Approval reads are last; the effect driver repeats this check directly
        # before the exact-head merge request after publishing its check.
        reviews = self.github.approvals(item, pr)
        observation = {'unit_id': item['unit_id'], 'candidate_id': identity['id'],
                       'reviews': reviews, 'observed_at': self.clock()}
        if any(review['state'] == 'CHANGES_REQUESTED' for review in reviews):
            return result(None)
        human = any(review['counted'] for review in reviews)
        if required and not human:
            return result(None)
        return result(dict(verified=True, merge_enabled=True, approved=True, checks_passed=True, protection_ready=True,
            candidate_id=identity['id'], head_oid=identity['head_oid'], base_oid=identity['base_oid'],
            policy_hash=identity['policy_hash'], requirements_hash=identity['requirements_hash'],
            observed_at=self.clock(), human_approved=human))


    def evaluate(self, effect):
        return self.evaluate_observation(effect)['authorization']

    __call__ = evaluate


class MergeController:
    def __init__(self, store, *, admission, clock=time.time):
        self.store, self.admission, self.clock = store, admission, clock
        self.effects = Effects(store, clock=clock)
        self.queries = Queries(store)
        self.commands = Lifecycle(store, clock=clock)

    def step(self, unit_id):
        item = candidate_snapshot(self.store, unit_id)
        if item is None or item['state'] != 'merging':
            return {'unit_id': unit_id, 'pending': 'technical_evidence'}
        with self.store.read_snapshot() as db:
            if db.execute("SELECT 1 FROM store_metadata WHERE key='import_only' AND value='true'").fetchone():
                raise TransitionError('staging imports cannot request merges')
            row = db.execute("SELECT effect_id FROM merge_requests WHERE unit_id=? AND candidate_id=? "
                             "AND status IN ('pending','uncertain','merged') LIMIT 1", (unit_id, item['candidate_id'])).fetchone()
        if row:
            return {'unit_id': unit_id, 'effect_id': row[0]}
        if isinstance(self.admission, MergeAdmission):
            evaluated = self.admission.evaluate_observation(item)
            authorization = evaluated['authorization']
            observation = evaluated['approval_observation']
            if observation is not None:
                from ..store.presentation import record_approval_display
                from ..store.runtime import writable
                with self.store.write() as db:
                    writable(db)
                    if db.execute('SELECT 1 FROM units WHERE id=? AND current_candidate_id=?',
                            (observation['unit_id'], observation['candidate_id'])).fetchone():
                        record_approval_display(db, observation['candidate_id'],
                            observation['reviews'], observation['observed_at'])
        else:
            authorization = self.admission(item)
        if authorization is None:
            return {'unit_id': unit_id, 'pending': 'authorization'}
        # A human-required audit creates a gate in the same commit as its pass.
        # Only a freshly counted GitHub approval can resolve that gate here.
        for gate in item['gates']:
            if gate['code'] == 'approval_required' and gate['resolved_at'] is None:
                if authorization.get('human_approved') is not True:
                    return {'unit_id': unit_id, 'pending': 'human_approval'}
                unit = self.queries.unit(unit_id)
                self.commands.resolve_gate(Command(str(uuid.uuid4()), 'supervisor:approval', unit_id, unit['version']),
                    gate['id'], release='evidence', evidence=authorization)
        unit = self.queries.unit(unit_id)
        command = Command(str(uuid.uuid4()), 'supervisor:merge', unit_id, unit['version'])
        return {'unit_id': unit_id, **self.effects.queue_merge(command, item['candidate_id'],
            policy_hash=item['identity']['policy_hash'], authorization=authorization)}

    def cycle(self, *, after=None, limit=50):
        page = self.queries.units(after=after, limit=limit)
        results = []
        for unit in page['items']:
            if unit['state'] != 'merging':
                continue
            try:
                results.append(self.step(unit['id']))
            except (GitHubUnavailable, TransitionError, StoreError, OSError) as error:
                results.append({'unit_id': unit['id'], 'error': getattr(error, 'code', type(error).__name__)})
        return {'items': results, 'next_cursor': page['next_cursor']}
