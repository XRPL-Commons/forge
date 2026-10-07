"""Bounded SQLite singleton dispatch. No engine start or credential lookup here.

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


class GitHubPrepare:
    """Fresh remote admission and exact worker inputs for the singleton adapter."""
    def __init__(self, github, config):
        self.github, self.config = github, validate(config)

    def __call__(self, snapshot, role):
        unit, member, issue = snapshot['unit'], snapshot['member'], snapshot['issue']
        candidate = snapshot['candidate']
        item = dict(repo_id=unit['repository_id'], repo=issue['repository'], number=issue['github_number'],
                    candidate={'pr': candidate['pull_number']} if candidate else None)
        remote, requirements = self.github.issue(item)
        pinned = issue['requirements']
        # Imported legacy requirement records need explicit reconciliation.
        expected = pinned if isinstance(pinned, str) else None
        if remote.get('state') != 'open' or requirements != expected:
            return None
        labels = {label['name'] if isinstance(label, dict) else label for label in remote.get('labels', [])}
        if labels.intersection(self.config['hold_labels']) or ownership(remote, item, item['repo'], self.github.pulls(item)):
            return None
        # Dependency evaluation is a separate ordered observation. Until that
        # adapter exists, do not infer completion from issue text or stale facts.
        if issue_dependencies(remote.get('body'), item):
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
        item['environment_choice'] = choose(self.config, item, remote)
        config = worker_config(self.config, item)
        memory = snapshot.get('memory_bytes', {}).get(role)
        if memory:
            from .capacity import memory_bytes
            if memory > memory_bytes(config['memory']):
                if memory % (1024 ** 2):
                    raise TransitionError('memory profile must use whole megabytes')
                config['memory'] = str(memory // (1024 ** 2)) + 'm'
        publisher = self.github.publisher(role)
        context = dict(review_policy=policy(config), verification_checks=config['repository_verification'].get(item['repo'], []))
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
        return PreparedDispatch(base, publisher, config, branch, context)


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
            members = db.execute('SELECT * FROM unit_issues WHERE unit_id=? AND left_at IS NULL LIMIT 2', (unit_id,)).fetchall()
            history = db.execute('SELECT id FROM unit_issues WHERE unit_id=? LIMIT 2', (unit_id,)).fetchall()
            if len(members) != 1 or len(history) != 1:
                return {'unit': unit, 'reason': 'singleton_required'}
            member = dict(members[0])
            row = db.execute('SELECT i.*,r.title,r.body,r.requirements_json,p.full_name AS repository '
                             'FROM issues i JOIN issue_revisions r ON r.id=? '
                             'JOIN repositories p ON p.id=i.repository_id WHERE i.id=?',
                             (member['requirements_revision_id'], member['issue_id'])).fetchone()
            issue = dict(row)
            issue['requirements'] = json.loads(issue.pop('requirements_json'))
            for key in ('labels', 'assignees', 'assignee_types'):
                issue[key] = json.loads(issue.pop(key + '_json'))
            candidate = db.execute('SELECT * FROM candidates WHERE id=?', (unit['current_candidate_id'],)).fetchone()
            role = {'queued': 'build', 'building': 'build', 'reviewing': 'review', 'auditing': 'audit'}.get(unit['state'])
            gates = applicable_gates(db, unit, member['id'] if role == 'build' else None, unit['current_candidate_id'])
            reason = gates[0]['code'] if gates else None
            if db.execute('SELECT 1 FROM jobs WHERE unit_id=? AND cleanup=0', (unit_id,)).fetchone():
                reason = 'cleanup_pending'
            if role is None:
                reason = reason or 'phase_ineligible'
            environment = db.execute('SELECT * FROM unit_environment_observations WHERE unit_id=?', (unit_id,)).fetchone()
            environment = dict(environment) if environment else {}
            for key in ('choice', 'override', 'retry', 'memory_retry'):
                environment[key] = json.loads(environment.pop(key + '_json') or 'null') if key + '_json' in environment else None
            memory = {}
            for row in db.execute('SELECT role,memory_bytes FROM memory_profiles WHERE repository_id=? ORDER BY revision', (unit['repository_id'],)):
                memory[row['role']] = row['memory_bytes']
            previous = db.execute('SELECT l.branch FROM jobs j JOIN job_launches l ON l.job_id=j.id '
                                  "WHERE j.unit_id=? AND j.role='build' ORDER BY j.created_at DESC,j.id DESC LIMIT 1", (unit_id,)).fetchone()
            return dict(unit=unit, member=member, issue=issue, candidate=dict(candidate) if candidate else None,
                        role=role, reason=reason, environment=environment, memory_bytes=memory,
                        resume_branch=previous[0] if previous else None)

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
        labels = {row['name'] if isinstance(row, dict) else row for row in issue['labels']}
        if labels.intersection(self.config['hold_labels']):
            return {**result, 'reason': 'label_hold'}
        if ownership(issue, {'number': issue['github_number']}, issue['repository']):
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
        config = deepcopy(prepared.runner_config)
        # Runtime files contain credential references only. Reject unknown keys
        # rather than persisting environment values supplied by a caller.
        extras = {key: config.pop(key) for key in ('environment_tier', 'services') if key in config}
        config = {**validate(config), **extras}
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
        launch_policy = policy(config)
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

    def cycle(self, *, after=None, limit=50):
        # Default scans remember their page across turns and controller restarts.
        # Role priority applies within each bounded page, so an old blocked unit
        # cannot indefinitely hide later ready work. Explicit cursors are read-only.
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
            rows = db.execute("SELECT id,state FROM units WHERE id>? AND state IN ('queued','building','reviewing','auditing') "
                              'ORDER BY id LIMIT ?', (after, limit + 1)).fetchall()
        page = rows[:limit]
        priority = {'auditing': 0, 'reviewing': 1, 'building': 2, 'queued': 2}
        items = []
        for row in sorted(page, key=lambda row: priority[row['state']]):
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
