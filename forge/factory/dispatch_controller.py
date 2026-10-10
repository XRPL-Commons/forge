"""Bounded SQLite delivery dispatch. No engine start or credential lookup here.

Preparation does remote reads before reservation; lifecycle versions and scoped
SQL gates are checked again in the reservation transaction. Provider account
leases are owned by immutable launches until verified cleanup.
"""
from copy import deepcopy
from dataclasses import dataclass, field
import json
import time
from urllib.parse import quote
import uuid

from ..store.db import StoreError
from ..store.executions import Launch, provider_available
from ..store.models import Command, TransitionError
from ..store.queries import _limit
from ..store.transitions import Lifecycle, applicable_gates, manifest_hash
from .config import policy, validate
from .contracts import SHA, issue_dependencies
from .github import GitHubUnavailable
from .ownership import ownership
from .runner import ContainerRunner, RunnerError


@dataclass(frozen=True)
class PreparedDispatch:
    base_oid: str
    publisher: str
    runner_config: dict
    branch: str | None = None
    context: dict = field(default_factory=dict)
    unit_version: int | None = None
    member_choices: dict | None = None


def memory_config(config, role, profile, environments):
    """One allocation authority for a physical execution's exact issue scope."""
    from .capacity import memory_bytes
    tiers = config['memory_tiers']
    retries = [state.get('memory_retry') or {} for state in environments]
    requested = [memory_bytes(retry['memory']) for retry in retries if retry.get('role') == role] if tiers else []
    if requested and max(requested) > memory_bytes(tiers[-1]):
        return None
    memory = max([profile, *requested])
    if memory and tiers:
        memory = memory_bytes(next((tier for tier in tiers if memory_bytes(tier) >= memory), tiers[-1]))
    if memory > memory_bytes(config['memory']):
        if memory % (1024 ** 2):
            raise TransitionError('memory profile must use whole megabytes')
        config = {**config, 'memory': str(memory // (1024 ** 2)) + 'm'}
    return config


class GitHubPrepare:
    """Fresh remote admission and exact worker inputs for each assigned scope."""
    def __init__(self, github, config, *, requirements=None):
        self.github, self.config = github, validate(config)
        self.requirements = requirements

    def __call__(self, snapshot, role):
        if snapshot.get('grouped'):
            return self.group(snapshot, role)
        unit, member, issue = snapshot['unit'], snapshot['member'], snapshot['issue']
        candidate = snapshot['candidate']
        item = dict(repo_id=unit['repository_id'], repo=issue['repository'], number=issue['github_number'],
                    candidate={'pr': candidate['pull_number'],'key':candidate['id']} if candidate else None,unit_state=unit['state'],reviewers_requested=snapshot.get('reviewers_requested'))
        remote, requirements = self.github.issue(item)
        pinned = issue['requirements']
        # Imported legacy requirement records need explicit reconciliation.
        expected = pinned if isinstance(pinned, str) else None
        if remote.get('state') != 'open' or requirements != expected:
            return None
        labels = {label['name'] if isinstance(label, dict) else label for label in remote.get('labels', [])}
        if labels.intersection(self.config['hold_labels']) or ownership(remote, item, item['repo'], self.github.pulls(item)):
            return None
        # The requirements adapter independently refreshes prerequisite facts.
        # A caller without that adapter cannot infer completion from issue text.
        refreshed = self.requirements.prepare(snapshot, role, remote) if self.requirements else None
        if self.requirements and refreshed is None:
            return None
        if not self.requirements and issue_dependencies(remote.get('body'), item):
            return None
        target = self.github.target(item)
        if target != unit['base_branch'] or not self.github.protections(item, {'base_ref': target}):
            return None
        base = (self.github.call('controller', item, 'GET', '/branches/' + quote(target, safe='')).get('commit') or {}).get('sha')
        if not isinstance(base, str) or not SHA.fullmatch(base):
            raise GitHubUnavailable('Target branch head could not be verified')
        if candidate and candidate['base_oid'] != base:
            return None
        from .environments import choose, worker_config
        environment = snapshot.get('environment') or {}
        item.update(environment_choice=environment.get('choice'), environment_override=environment.get('override'),
                    environment_retry=environment.get('retry'), environment_label=environment.get('label'))
        item['environment_choice'] = refreshed['choice'] if refreshed else choose(self.config, item, remote)
        config = worker_config(self.config, item)
        config = memory_config(config, role, snapshot.get('memory_bytes', {}).get(role, 0), [environment])
        if config is None:
            return None
        publisher = self.github.publisher(role)
        context = dict(review_policy=policy(config, item['repo_id'], repository_name=item['repo']), verification_checks=config['repository_verification'].get(item['repo'], []))
        ci_item = {**item, 'candidate': dict(head=candidate['head_oid'], base=candidate['base_oid']) if candidate else None}
        context['ci_evidence'] = self.github.ci_evidence(ci_item)
        branch = candidate['branch'] if candidate else None
        if role == 'build':
            draft = self.github.own_draft(item, issue['github_number'], publisher)
            retry = environment.get('memory_retry') or environment.get('retry') or {}
            branch = draft['branch'] if draft else retry.get('branch') or snapshot.get('resume_branch')
            if branch:
                context['resume_branch'] = branch
            if draft:
                context['resume_pr'] = draft['pr']
        return PreparedDispatch(base, publisher, config, branch, context, refreshed['unit_version'] if refreshed else None)

    def group(self, snapshot, role):
        unit, candidate = snapshot['unit'], snapshot['candidate']
        # Repository identity is shared; no issue is selected as group authority.
        item = {'repo_id': unit['repository_id'], 'repo': snapshot['members'][0]['issue']['repository']}
        for key in ('publication_transfer', 'approved_sources', 'owned_publication'):
            if key in snapshot:
                item[key] = snapshot[key]
        remotes = {}
        for scoped in snapshot['members']:
            issue, member = scoped['issue'], scoped['member']
            remote, requirements = self.github.issue({**item, 'number': issue['github_number']})
            if remote.get('state') != 'open' or requirements != issue['requirements']:
                return None
            remotes[member['id']] = remote
        if self.requirements:
            refreshed = self.requirements.prepare_group(snapshot, role, remotes)
            if refreshed is None:
                return None
        else:
            refreshed = None
            numbers = {scope['issue']['github_number'] for scope in snapshot['members']}
            for scoped in snapshot['members']:
                remote = remotes[scoped['member']['id']]
                labels = {row['name'] if isinstance(row, dict) else row for row in remote.get('labels', [])}
                if labels.intersection(self.config['hold_labels']) or set(issue_dependencies(remote.get('body'))) - numbers:
                    return None
                member_item = {**item, 'number': scoped['issue']['github_number'], 'unit_state': unit['state'],
                               'candidate': {'pr': candidate['pull_number'], 'key': candidate['id']} if candidate else None}
                if ownership(remote, member_item, item['repo'], self.github.pulls(item)):
                    return None
        target = self.github.target(item)
        if target != unit['base_branch'] or not self.github.protections(item, {'base_ref': target}):
            return None
        base = (self.github.call('controller', item, 'GET', '/branches/' + quote(target, safe='')).get('commit') or {}).get('sha')
        if not isinstance(base, str) or not SHA.fullmatch(base):
            raise GitHubUnavailable('Target branch head could not be verified')
        if candidate and candidate['base_oid'] != base:
            return None
        config = deepcopy(self.config)
        context = {'review_policy': policy(config, item['repo_id'], repository_name=item['repo']), 'verification_checks': config['repository_verification'].get(item['repo'], []),
                   'ci_evidence': self.github.ci_evidence({**item, 'candidate': dict(head=candidate['head_oid'], base=candidate['base_oid']) if candidate else None})}
        transfer = snapshot.get('publication_transfer')
        if transfer and role == 'build':
            from .ownership import publication_transfer_matches
            current = self.github.pr(item, transfer['pull_number'])
            if not publication_transfer_matches(current, item, allow_publication=False):
                return None
            context.update(resume_branch=transfer['branch'], resume_pr=transfer['pull_number'],
                           resume_head=transfer['expected_head'], publication_author=transfer['author'])
        elif snapshot.get('resume_candidate') and role == 'build':
            previous = snapshot['resume_candidate']
            current = self.github.pr(item, previous['pull_number'])
            if (current.get('head', {}).get('sha') != previous['head_oid']
                    or current.get('head', {}).get('ref') != previous['branch']
                    or current.get('head', {}).get('repo', {}).get('id') != unit['repository_id']
                    or current.get('base', {}).get('ref') != unit['base_branch']
                    or current.get('base', {}).get('repo', {}).get('id') != unit['repository_id']
                    or current.get('state') != 'open' or current.get('merged')
                    or current.get('user', {}).get('login') != self.github.publisher('build')
                    or current.get('user', {}).get('type') != 'Bot'):
                return None
            context.update(resume_branch=previous['branch'], resume_pr=previous['pull_number'],
                           resume_head=previous['head_oid'], publication_author={key: current.get('user', {}).get(key) for key in ('id', 'login', 'type')})
        return PreparedDispatch(base, self.github.publisher(role), config, context.get('resume_branch') or (candidate['branch'] if candidate else None),
                                context, refreshed['unit_version'] if refreshed else None,
                                refreshed.get('choices', {}) if refreshed else None)


class DispatchController:
    def __init__(self, store, runner, *, config, prepare, admission, clock=time.time):
        self.store, self.runner = store, runner
        self.config = validate(config)
        self.prepare, self.admission, self.clock = prepare, admission, clock
        self.commands = Lifecycle(store, clock=clock)

    def snapshot(self, unit_id):
        with self.store.read_snapshot() as db:
            if db.execute("SELECT 1 FROM store_metadata WHERE key='import_only' AND value='true'").fetchone():
                raise TransitionError('staging imports cannot dispatch workers')
            row = db.execute('SELECT * FROM units WHERE id=?', (unit_id,)).fetchone()
            if row is None:
                raise KeyError(unit_id)
            unit = dict(row)
            rows = db.execute('SELECT * FROM unit_issues WHERE unit_id=? AND left_at IS NULL ORDER BY id', (unit_id,)).fetchall()
            members = []
            for member_row in rows:
                member = dict(member_row)
                row = db.execute('SELECT i.*,r.title,r.body,r.requirements_json,p.full_name AS repository '
                                 'FROM issues i JOIN issue_revisions r ON r.id=? '
                                 'JOIN repositories p ON p.id=i.repository_id WHERE i.id=?',
                                 (member['requirements_revision_id'], member['issue_id'])).fetchone()
                issue = dict(row)
                issue['requirements'] = json.loads(issue.pop('requirements_json'))
                for key in ('labels', 'assignees', 'assignee_types'):
                    issue[key] = json.loads(issue.pop(key + '_json'))
                environment = db.execute('SELECT * FROM issue_environment_observations WHERE issue_id=?', (member['issue_id'],)).fetchone()
                environment = dict(environment) if environment else {}
                for key in ('choice', 'override', 'retry', 'memory_retry'):
                    environment[key] = json.loads(environment.pop(key + '_json') or 'null') if key + '_json' in environment else None
                members.append({'member': member, 'issue': issue, 'environment': environment})
            if not members:
                return {'unit': unit, 'reason': 'membership_empty'}
            grouped = db.execute('SELECT 1 FROM unit_publication_targets WHERE unit_id=?', (unit_id,)).fetchone() is not None
            member, issue = (None, None) if grouped else (members[0]['member'], members[0]['issue'])
            candidate = db.execute('SELECT * FROM candidates WHERE id=?', (unit['current_candidate_id'],)).fetchone()
            role = {'queued': 'build', 'building': 'build', 'integrating': 'build', 'reviewing': 'review', 'auditing': 'audit'}.get(unit['state'])
            gates = applicable_gates(db, unit, member['id'] if member and role == 'build' else None, unit['current_candidate_id'])
            if grouped and role == 'build':
                gates = [gate for gate in gates if gate['member_id'] is None and gate['code'] not in {'cleanup_pending', 'result_pending'}]
            reason = gates[0]['code'] if gates else None
            if not grouped and db.execute('SELECT 1 FROM jobs WHERE unit_id=? AND cleanup=0', (unit_id,)).fetchone():
                reason = 'cleanup_pending'
            if role is None:
                reason = reason or 'phase_ineligible'
            environment = {} if grouped else members[0]['environment']
            memory = {}
            for row in db.execute('SELECT role,memory_bytes FROM memory_profiles WHERE repository_id=? ORDER BY revision', (unit['repository_id'],)):
                memory[row['role']] = row['memory_bytes']
            previous = db.execute('SELECT l.branch FROM jobs j JOIN job_launches l ON l.job_id=j.id '
                                  "WHERE j.unit_id=? AND j.role='build' ORDER BY j.created_at DESC,j.id DESC LIMIT 1", (unit_id,)).fetchone()
            from ..store.operator_views import reviewer_request_in
            from .ownership import publication_context
            ownership_context = publication_context(self.store, unit_id, {})
            from ..store.delivery_inputs import previous_publication_in
            resume_candidate = previous_publication_in(db, unit_id) if grouped else None
            return dict(**ownership_context, reviewers_requested=reviewer_request_in(db,unit['current_candidate_id']),unit=unit, member=member, issue=issue, candidate=dict(candidate) if candidate else None,
                        role=role, reason=reason, environment=environment, memory_bytes=memory, members=members, grouped=grouped, repo=members[0]['issue']['repository'],
                        resume_branch=previous[0] if previous else None, resume_candidate=resume_candidate)

    def _provider(self, config):
        pool = config['provider_pool'] or [dict(id=None, **{key: config[key] for key in
                ('provider', 'model', 'provider_environment', 'provider_auth_file_env')})]
        with self.store.read_snapshot() as db:
            cursor = db.execute("SELECT cursor FROM runtime_controls WHERE name='provider_cursor'").fetchone()
            start = int(cursor[0]) % len(pool) if cursor and cursor[0] else 0
            for offset in range(len(pool)):
                entry = pool[(start + offset) % len(pool)]
                if config['provider_ready'] is not None and entry['id'] not in config['provider_ready']:
                    continue
                selected = {**config, **{key: value for key, value in entry.items() if key != 'id'}}
                if provider_available(db, entry['provider'], entry['id'], selected, self.clock()):
                    return entry, selected
        return None, None

    def step(self, unit_id, role=None):
        result = {'unit_id': unit_id, 'reserved': False}
        if not self.config['enabled']:
            return {**result, 'reason': 'runtime_disabled'}
        snapshot = self.snapshot(unit_id)
        if snapshot['reason']:
            return {**result, 'reason': snapshot['reason']}
        if role is not None and role != snapshot['role']:
            return {**result, 'reason': 'phase_ineligible'}
        role = snapshot['role']
        if self.admission(snapshot) is not True:
            return {**result, 'reason': 'repository_setup'}
        # Cached human/hold facts fail closed even with a custom prepare adapter.
        issue = snapshot['issue']
        if not snapshot['grouped']:
            labels = {row['name'] if isinstance(row, dict) else row for row in issue['labels']}
            if labels.intersection(self.config['hold_labels']):
                return {**result, 'reason': 'label_hold'}
            if ownership(issue, {'number':issue['github_number'],'unit_state':snapshot['unit']['state'],
                'candidate':{'pr':snapshot['candidate']['pull_number'],'key':snapshot['candidate']['id']} if snapshot['candidate'] else None,
                'reviewers_requested':snapshot.get('reviewers_requested')},issue['repository']):
                return {**result, 'reason': 'human_owned'}
        with self.store.read_snapshot() as db:
            occupied = {row[0] for row in db.execute('SELECT slot FROM job_leases UNION SELECT slot FROM discussion_leases')}
        slot = next((f'{role}-{index + 1}' for index in range(self.config['slots'][role])
                     if f'{role}-{index + 1}' not in occupied), None)
        if slot is None:
            return {**result, 'reason': 'role_capacity'}
        prepared = self.prepare(deepcopy(snapshot), role)
        if prepared is None:
            return {**result, 'reason': 'observation_changed'}
        if not isinstance(prepared, PreparedDispatch) or not isinstance(prepared.base_oid, str) or not SHA.fullmatch(prepared.base_oid):
            raise TransitionError('dispatch requires verified target head and launch inputs')
        if prepared.unit_version is not None:
            refreshed = self.snapshot(unit_id)
            if (refreshed['reason'] or refreshed['unit']['version'] != prepared.unit_version
                    or [(row['member'], row['issue']) for row in refreshed['members']] != [(row['member'], row['issue']) for row in snapshot['members']] or refreshed['candidate'] != snapshot['candidate']
                    or refreshed['role'] != role
                    or refreshed['unit']['fencing_generation'] != snapshot['unit']['fencing_generation']):
                return {**result, 'reason': 'observation_changed'}
            snapshot = refreshed
        assignment = None
        if snapshot['grouped'] and role == 'build':
            from ..store.delivery import DeliveryQueries
            selection = DeliveryQueries(self.store).assignments(unit_id, base_oid=prepared.base_oid,
                                                               policy_hash=manifest_hash(policy(prepared.runner_config, snapshot['unit']['repository_id'], repository_name=snapshot['repo'])),
                                                               allowed_members=set(prepared.member_choices) if prepared.member_choices is not None else None)
            if selection['version'] != snapshot['unit']['version']:
                return {**result, 'reason': 'observation_changed'}
            choices = prepared.member_choices
            def ready(assignment):
                if choices is None:
                    return True
                if assignment['stage'] == 'child':
                    return assignment['member_id'] in choices
                if assignment['stage'] == 'planning':
                    return bool(choices)
                return all(row['member']['id'] in choices for row in snapshot['members'])
            assignment = next((item for item in selection['items'] if ready(item)), None)
            if assignment is None:
                return {**result, 'reason': selection['pending_reason'] or 'member_blocked'}
        if snapshot['grouped'] and role != 'build' and prepared.member_choices is not None:
            if any(row['member']['id'] not in prepared.member_choices for row in snapshot['members']):
                return {**result, 'reason': 'member_blocked'}
        config = deepcopy(prepared.runner_config)
        if snapshot['grouped'] and prepared.member_choices:
            from .environments import worker_config
            choices = ([prepared.member_choices[assignment['member_id']]] if assignment and assignment.get('member_id')
                       else list(prepared.member_choices.values()))
            services = sorted({service for choice in choices for service in choice.get('services', [])})
            choice = {'tier': 'full' if any(choice['tier'] == 'full' for choice in choices) else 'standard', 'services': services}
            config = worker_config(config, {'environment_choice': choice})
        if snapshot['grouped']:
            scope = [row for row in snapshot['members'] if not assignment or assignment['stage'] != 'child'
                     or row['member']['id'] == assignment['member_id']]
            config = memory_config(config, role, snapshot.get('memory_bytes', {}).get(role, 0),
                                   [row['environment'] for row in scope])
            if config is None:
                return {**result, 'reason': 'memory_unavailable'}
        # Runtime files contain credential references only. Reject unknown keys
        # rather than persisting environment values supplied by a caller.
        extras = {key: config.pop(key) for key in ('environment_tier', 'services') if key in config}
        allocation = config['memory']
        if config['memory_tiers']:
            from .capacity import memory_bytes
            if memory_bytes(allocation) not in {memory_bytes(tier) for tier in config['memory_tiers']} and extras.get('environment_tier') != 'full':
                raise TransitionError('worker allocation must match a configured memory tier')
            config['memory'] = config['memory_tiers'][0]
        config = {**validate(config), **extras, 'memory': allocation}
        if not config['enabled']:
            return {**result, 'reason': 'runtime_disabled'}
        entry, config = self._provider(config)
        if entry is None:
            return {**result, 'reason': 'provider_unavailable'}
        runner = ContainerRunner(config, environ=self.runner.environ) if isinstance(self.runner, ContainerRunner) else self.runner
        runner.ready()
        capacity = runner.capacity()
        if capacity.get('allowed') is not True:
            return {**result, 'reason': 'capacity_unavailable'}
        if self.admission(snapshot) is not True:
            return {**result, 'reason': 'repository_setup'}
        unit, member, candidate = snapshot['unit'], snapshot['member'], snapshot['candidate']
        if snapshot['grouped'] and role == 'build':
            return self._reserve_group(snapshot, prepared, assignment, config, entry, slot, result)
        launch_policy = policy(config, unit['repository_id'], repository_name=snapshot['repo'])
        if role == 'build':
            manifest = dict(requirements_revision_id=member['requirements_revision_id'], base_oid=prepared.base_oid,
                            policy_hash=manifest_hash(launch_policy))
        else:
            if not candidate or candidate['base_oid'] != prepared.base_oid or candidate['policy_hash'] != manifest_hash(launch_policy):
                return {**result, 'reason': 'candidate_changed'}
            manifest = {key: candidate[key] for key in ('head_oid', 'base_oid', 'policy_hash', 'requirements_hash', 'input_manifest_hash')}
            manifest['candidate_id'] = candidate['id']
        request = str(uuid.uuid4())
        branch = prepared.branch or (candidate['branch'] if candidate else f"forge/issue-{issue['github_number']}-{request[:8]}")
        launch = Launch(branch, prepared.publisher, entry['provider'], self.clock() + config['timeout_seconds'][role],
                        config, launch_policy, entry['id'], entry['model'], prepared.context)
        reservation = self.commands.reserve_job(Command(request, 'supervisor:dispatch', unit_id, unit['version']), role,
            slot=slot, execution_fence=str(uuid.uuid4()), input_manifest=manifest,
            member_id=member['id'] if role == 'build' else None,
            candidate_id=candidate['id'] if role != 'build' else None, launch=launch)
        return {**result, 'reserved': True, 'job_id': reservation['job_id'], 'role': role}

    def _reserve_group(self, snapshot, prepared, assignment, config, entry, slot, result):
        from ..store.delivery import Delivery
        from ..harnesses.registry import manifest as harness_manifest
        if harness_manifest(entry['provider']).get('delegation') != 'managed_children':
            return {**result, 'reason': 'delegation_unsupported'}
        request = str(uuid.uuid4())
        unit = snapshot['unit']
        branch = prepared.branch or ('forge/unit-' + unit['id'] + '-' + request[:8])
        context = dict(prepared.context)
        target = assignment['input_manifest'].get('publication_target')
        if assignment['stage'] == 'integration' and target and target['kind'] == 'existing':
            source = next(row for row in assignment['input_manifest']['existing_candidates'] if row['candidate_id'] == target['candidate_id'])
            branch = source['branch']
            context.update(resume_branch=branch, resume_pr=target['pull_number'])
        launch = Launch(branch, prepared.publisher, entry['provider'], self.clock() + config['timeout_seconds']['build'],
                        config, policy(config, unit['repository_id'], repository_name=snapshot['repo']), entry['id'], entry['model'], context)
        command = Command(request, 'supervisor:dispatch', unit['id'], unit['version'])
        options = dict(slot=slot, execution_fence=str(uuid.uuid4()), input_manifest=assignment['input_manifest'], launch=launch)
        delivery = Delivery(self.store, clock=self.clock)
        if assignment.get('plan_id'):
            reservation = delivery.delegate(command, assignment['plan_id'], assignment['key'],
                                           worktree_key=assignment['worktree_key'], **options)
        else:
            reservation = delivery.reserve_builder(command, stage=assignment['stage'], **options)
        return {**result, 'reserved': True, 'job_id': reservation['job_id'], 'role': 'build',
                'builder_stage': assignment.get('stage'), 'delegation_key': assignment.get('key')}

    def cycle(self, *, after=None, limit=50):
        # Default scans remember their page across turns and controller restarts.
        # Within a page, same-role deliveries share turns in successful reservation
        # order. Atomic reservation events retain that order across restarts, without
        # advancing it for blocked visits or failed reservations. Role priority and
        # the bounded traversal cursor remain independent of allocation order.
        limit = _limit(limit)
        automatic = after is None
        if after is not None and not isinstance(after, str):
            raise ValueError('dispatch cursor must be a unit ID')
        if not self.config['enabled']:
            return {'items': [], 'next_cursor': None}
        with self.store.read_snapshot() as db:
            if db.execute("SELECT 1 FROM store_metadata WHERE key='import_only' AND value='true'").fetchone():
                raise TransitionError('staging imports cannot dispatch workers')
            if automatic:
                cursor = db.execute("SELECT cursor FROM runtime_controls WHERE name='dispatch_cursor'").fetchone()
                after = cursor[0] if cursor and cursor[0] else ''
            rows = db.execute("SELECT u.id,u.state,coalesce((SELECT e.sequence FROM events e "
                "JOIN jobs j ON j.id=e.job_id JOIN job_launches l ON l.job_id=j.id "
                "WHERE e.unit_id=u.id AND e.kind='attempt_reserved' AND j.role=CASE u.state "
                "WHEN 'reviewing' THEN 'review' WHEN 'auditing' THEN 'audit' ELSE 'build' END "
                "ORDER BY e.sequence DESC LIMIT 1),0) AS allocation_sequence FROM units u "
                "WHERE u.id>? AND u.state IN ('queued','building','integrating','reviewing','auditing') "
                'ORDER BY u.id LIMIT ?', (after, limit + 1)).fetchall()
        # An eligible memory escalation drains capacity; smaller work must not
        # refill it while the next bounded allocation is waiting to fit.
        if self.config['memory_tiers']:
            with self.store.read_snapshot() as db:
                retries = db.execute("SELECT u.id FROM units u JOIN unit_issues m ON m.unit_id=u.id AND m.left_at IS NULL JOIN issue_environment_observations e ON e.issue_id=m.issue_id "
                    "WHERE e.memory_retry_json IS NOT NULL AND u.state IN ('queued','building','integrating','reviewing','auditing') "
                    "ORDER BY json_extract(e.memory_retry_json,'$.at'),u.id LIMIT 100").fetchall()
            for retry in retries:
                snapshot = self.snapshot(retry['id'])
                if (not snapshot['reason'] and snapshot['role'] == (snapshot['environment'].get('memory_retry') or {}).get('role')
                        and self.admission(snapshot) is True):
                    try:
                        result = self.step(retry['id'])
                    except (RunnerError, GitHubUnavailable, TransitionError, StoreError, OSError) as error:
                        result = {'unit_id': retry['id'], 'error': getattr(error, 'code', type(error).__name__)}
                    if result.get('reserved') or result.get('reason') in {'capacity_unavailable','role_capacity'}:
                        return {'items': [result], 'next_cursor': after or None}
        page = rows[:limit]
        priority = {'auditing': 0, 'reviewing': 1, 'building': 2, 'integrating': 2, 'queued': 2}
        items = []
        for row in sorted(page, key=lambda row: (priority[row['state']], row['allocation_sequence'], row['id'])):
            try:
                items.append(self.step(row['id']))
            except (RunnerError, GitHubUnavailable, TransitionError, StoreError, OSError) as error:
                items.append({'unit_id': row['id'], 'error': getattr(error, 'code', type(error).__name__)})
        cursor = page[-1]['id'] if len(rows) > limit else None
        if automatic:
            with self.store.write() as db:
                db.execute("INSERT INTO runtime_controls(name,cursor,updated_at) VALUES('dispatch_cursor',?,?) "
                           'ON CONFLICT(name) DO UPDATE SET cursor=excluded.cursor,updated_at=excluded.updated_at, '
                           'generation=runtime_controls.generation+1', (cursor, self.clock()))
        return {'items': items, 'next_cursor': cursor}
