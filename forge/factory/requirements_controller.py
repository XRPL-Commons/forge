"""Fresh member prerequisites, durable worker choices and bounded retries.

GitHub reads and label projection run outside transactions. Ordered observations
and immutable launch records bind every decision to its requirements revision.
"""
import json
import time
import uuid

from ..store.db import StoreError
from ..store.effects import Effects, effect_due, retry_policy
from ..store.models import Command, TransitionError
from ..store.queries import _limit
from ..store.runtime import writable
from ..store.runtime_requirements import RuntimeRequirements, _memory
from ..store.transitions import manifest_hash
from .config import validate
from .effect_controller import EffectController
from .environments import choose
from .github import GitHubUnavailable
from .ownership import ownership


class EnvironmentProjectionPending(Exception):
    """A current durable label effect owns the next bounded attempt."""


class EnvironmentSelectionError(ValueError):
    """One member's durable environment decision has no runnable choice."""


class RuntimeRequirementsController:
    def __init__(self, store, github, *, config, runner=None, clock=time.time):
        self.store, self.github, self.clock = store, github, clock
        self.config, self.runner = validate(config), runner
        self.commands = RuntimeRequirements(store, clock=clock)
        self.reliability = self.config.get('reliability', {})
        self.effects = Effects(store, clock=clock, **retry_policy(self.reliability))

    def _snapshot(self, unit_id, member_id=None):
        # Share the bounded row projection, without invoking its dispatcher.
        from .dispatch_controller import DispatchController
        snapshot=DispatchController(self.store, self.runner, config=self.config,
            prepare=None, admission=lambda _: False, clock=self.clock).snapshot(unit_id)
        return self._scoped(snapshot, member_id) if member_id is not None else snapshot

    def _scoped(self, snapshot, member_id):
        scoped=next(row for row in snapshot['members'] if row['member']['id']==member_id)
        with self.store.read_snapshot() as db:
            row=db.execute('SELECT * FROM issue_environment_observations WHERE issue_id=?',(scoped['issue']['id'],)).fetchone()
        environment=dict(row) if row else {}
        for key in ('choice','override','retry','memory_retry'):
            environment[key]=json.loads(environment.pop(key+'_json') or 'null') if key+'_json' in environment else None
        return {**snapshot,**scoped,'environment':environment}

    @staticmethod
    def _item(snapshot):
        item=dict(repo_id=snapshot['unit']['repository_id'], repo=snapshot['issue']['repository'],
                    number=snapshot['issue']['github_number'],unit_state=snapshot['unit']['state'],reviewers_requested=snapshot.get('reviewers_requested'),
                    candidate={'pr':snapshot['candidate']['pull_number'],'key':snapshot['candidate']['id']} if snapshot['candidate'] else None)
        return item

    def _command(self, snapshot):
        return Command(str(uuid.uuid4()), 'supervisor:requirements', snapshot['unit']['id'], snapshot['unit']['version'])

    def _generation(self, unit_id):
        with self.store.write() as db:
            writable(db)
            name = 'dependencies:' + unit_id
            db.execute('INSERT INTO runtime_controls(name,generation,updated_at) VALUES(?,1,?) '
                       'ON CONFLICT(name) DO UPDATE SET generation=generation+1,updated_at=excluded.updated_at',
                       (name, self.clock()))
            return db.execute('SELECT generation FROM runtime_controls WHERE name=?', (name,)).fetchone()[0]

    def _dependencies(self, snapshot, remote):
        from .prerequisites import collect
        observation = collect(self.store, self.github, unit_id=snapshot['unit']['id'],
            member_id=snapshot['member']['id'],
            requirements_revision_id=snapshot['member']['requirements_revision_id'], body=remote.get('body'))
        generation = self._generation(snapshot['unit']['id'])
        result = self.commands.dependencies(self._command(snapshot),
            requirements_revision_id=snapshot['member']['requirements_revision_id'],
            generation=generation, facts=observation['facts'])
        return {'dependencies_clear': result.get('clear', False),
                'external_dependencies_clear': not observation['external_blocked']}

    @staticmethod
    def _labels(remote):
        return sorted({row['name'] if isinstance(row, dict) else row for row in remote.get('labels', [])
                       if (row['name'] if isinstance(row, dict) else row).startswith('env:')})

    def _project(self, snapshot, remote):
        """Recover uncertain label delivery before evaluating explicit operator labels."""
        with self.store.read_snapshot() as db:
            effects = db.execute('SELECT e.*,l.revision,l.requirements_revision_id,l.member_id,l.target_label,l.before_json FROM external_effects e JOIN environment_label_effects l ON l.effect_id=e.id '
                "WHERE e.unit_id=? AND l.member_id=? AND e.status IN ('pending','running','uncertain') ORDER BY e.created_at,e.id LIMIT 20",
                (snapshot['unit']['id'],snapshot['member']['id'])).fetchall()
            latest = db.execute('SELECT MAX(revision) FROM unit_environment WHERE member_id=?', (snapshot['member']['id'],)).fetchone()[0]
        for effect in effects:
            labels = set(self._labels(remote))
            target = effect['target_label']
            if (effect['requirements_revision_id'] != snapshot['member']['requirements_revision_id']
                    or effect['revision'] != latest):
                self.commands.environment_receipt(effect['id'], status='superseded', observed_labels=sorted(labels))
                continue
            if labels == {target}:
                self.commands.environment_receipt(effect['id'], status='confirmed', observed_labels=sorted(labels))
                continue
            before = set(json.loads(effect['before_json']))
            if labels not in (before, before | {target}):
                self.commands.environment_receipt(effect['id'], status='superseded', observed_labels=sorted(labels))
                continue  # A changed operator label is never overwritten by an old intent.
            if not effect_due(effect,self.clock()) or self.effects.bounded(effect['id']):
                raise EnvironmentProjectionPending()
            self.commands.claim_environment(effect['id'], expected_version=snapshot['unit']['version'],reliability=self.reliability)
            try:
                self.github.environment_label(self._item(snapshot), remote, target)
                remote, requirements = self.github.issue(self._item(snapshot))
                if requirements != snapshot['issue']['requirements']:
                    raise GitHubUnavailable('Requirements changed during environment projection')
                self.commands.environment_receipt(effect['id'], status='confirmed', observed_labels=self._labels(remote))
            except GitHubUnavailable:
                self.commands.environment_unavailable(effect['id'],reliability=self.reliability)
                self.effects.bounded(effect['id'])
                raise
        return remote

    def _environment(self, snapshot, remote):
        environment = snapshot['environment']
        item = {**self._item(snapshot), 'environment_choice': environment.get('choice'),
                'environment_override': environment.get('override'), 'environment_retry': environment.get('retry'),
                'environment_label': environment.get('label')}
        try:
            selection = choose(self.config, item, remote)
        except ValueError as error:
            with self.store.read_snapshot() as db:
                existing = db.execute("SELECT 1 FROM unit_gates WHERE unit_id=? AND member_id=? AND code='environment' AND resolved_at IS NULL AND json_extract(details_json,'$.source')='runtime_selection'", (snapshot['unit']['id'],snapshot['member']['id'])).fetchone()
            if not existing:
                self.commands.environment_problem(self._command(snapshot),requirements_revision_id=snapshot['member']['requirements_revision_id'],reason=str(error))
            raise EnvironmentSelectionError(str(error)) from error
        # Include only non-secret environment policy in the durable decision hash.
        config_hash = manifest_hash({key: self.config[key] for key in ('environment_tiers', 'repository_environments', 'memory_tiers')})
        with self.store.read_snapshot() as db:
            previous = db.execute('SELECT * FROM unit_environment WHERE member_id=? ORDER BY revision DESC LIMIT 1', (snapshot['member']['id'],)).fetchone()
            selection_gate = db.execute("SELECT 1 FROM unit_gates WHERE unit_id=? AND member_id=? AND code='environment' AND resolved_at IS NULL AND json_extract(details_json,'$.source')='runtime_selection'", (snapshot['unit']['id'],snapshot['member']['id'])).fetchone()
        if (selection_gate or previous is None or previous['requirements_revision_id'] != snapshot['member']['requirements_revision_id']
                or previous['config_hash'] != config_hash or json.loads(previous['choice']) != selection
                or self._labels(remote) != ['env:' + selection['tier']]):
            self.commands.select_environment(self._command(snapshot),
                requirements_revision_id=snapshot['member']['requirements_revision_id'], choice=selection,
                labels=self._labels(remote), config_hash=config_hash)
            snapshot = self._snapshot(snapshot['unit']['id'],snapshot['member']['id'])
            self._project(snapshot, remote)
        return selection

    def _observe_member(self, snapshot):
        try:
            return self._observe_member_ready(snapshot)
        except EnvironmentProjectionPending:
            return {'pending':'environment_label'}
        except GitHubUnavailable:
            # A failed read is uncertainty, but never another physical send.
            with self.store.read_snapshot() as db:
                pending=list(db.execute('SELECT e.id FROM external_effects e JOIN environment_label_effects l ON l.effect_id=e.id '
                    "WHERE l.member_id=? AND e.status IN ('pending','running','uncertain') ORDER BY e.created_at,e.id LIMIT 20",(snapshot['member']['id'],)))
            for effect in pending:
                self.commands.environment_unavailable(effect['id'],reliability=self.reliability)
                self.effects.bounded(effect['id'])
            raise

    def _observe_member_ready(self, snapshot):
        remote,requirements=self.github.issue(self._item(snapshot))
        if remote.get('number')!=snapshot['issue']['github_number'] or 'pull_request' in remote:
            raise GitHubUnavailable('Current issue identity could not be verified')
        if remote.get('state')!='open' or requirements!=snapshot['issue']['requirements'] or not isinstance(requirements,str):
            return {'pending':'issue_reconciliation'}
        remote=self._project(snapshot,remote)
        readiness=self._dependencies(snapshot,remote)
        snapshot=self._snapshot(snapshot['unit']['id'],snapshot['member']['id'])
        selection=self._environment(snapshot,remote)
        return {**readiness,'choice':selection}

    def observe(self, unit_id):
        with EffectController(self.store,self.github,clock=self.clock)._lock():
            snapshot=self._snapshot(unit_id)
            if snapshot['unit']['state'] in {'merged','closed','dissolved'}:
                return {'unit_id':unit_id,'pending':'ineligible'}
            results={}
            for scoped in snapshot['members']:
                member_id=scoped['member']['id']
                results[member_id]=self._observe_member(self._snapshot(unit_id,member_id))
            result={'unit_id':unit_id,'members':results,'unit_version':self._snapshot(unit_id)['unit']['version']}
            if len(results)==1:
                result.update(next(iter(results.values())))
            return result

    def _prepare_member(self,snapshot):
        fresh,requirements=self.github.issue(self._item(snapshot))
        labels={row['name'] if isinstance(row,dict) else row for row in fresh.get('labels',[])}
        from .ownership import publication_context, active_publication_matches
        item=publication_context(self.store,snapshot['unit']['id'],self._item(snapshot))
        pulls = [pr for pr in self.github.pulls(item)
                 if not active_publication_matches(self.store, snapshot['unit']['id'], pr)]
        if (requirements!=snapshot['issue']['requirements'] or labels.intersection(self.config['hold_labels'])
                or ownership(fresh,item,item['repo'],pulls)):
            return None
        return self._observe_member(snapshot)

    def prepare(self,snapshot,role,remote):
        with EffectController(self.store,self.github,clock=self.clock)._lock():
            result=self._prepare_member(snapshot)
            if result and result.get('dependencies_clear'):
                return {**result,'unit_version':self._snapshot(snapshot['unit']['id'])['unit']['version']}
        return None

    def prepare_group(self,snapshot,role,remotes):
        choices={}
        with EffectController(self.store,self.github,clock=self.clock)._lock():
            for scoped in snapshot['members']:
                member_id=scoped['member']['id']
                try:
                    result=self._prepare_member(self._snapshot(snapshot['unit']['id'],member_id))
                except EnvironmentSelectionError:
                    continue  # Its durable member gate does not block independent task scopes.
                if result and result.get('external_dependencies_clear' if role == 'build' else 'dependencies_clear'):
                    choices[member_id]=result['choice']
            return {'unit_version':self._snapshot(snapshot['unit']['id'])['unit']['version'],'choices':choices}

    def classify(self, job, capture, report):
        selection, kind = None, None
        config = job['runner_config']
        if (capture.get('execution') or {}).get('oom_killed') is True:
            tiers = self.config['memory_tiers']
            wanted = next((tier for tier in tiers if _memory(tier) > _memory(config['memory'])), None)
            if wanted:
                kind, selection = 'memory', dict(memory=wanted, tiers=tiers)
                evidence = dict(confirmed_memory_exhaustion=True, capture_job_id=job['id'])
        elif report and report.get('outcome') == 'blocked_environment' and report.get('required_services'):
            with self.store.read_snapshot() as db:
                states=[dict(row) for row in db.execute('SELECT e.* FROM job_members j JOIN unit_issues m ON m.id=j.member_id '
                    'LEFT JOIN issue_environment_observations e ON e.issue_id=m.issue_id WHERE j.job_id=?',(job['id'],))]
            wanted = report['required_services']
            automatic=all(not state['override_json'] and not state['retry_json'] and json.loads(state['choice_json'] or '{}').get('automatic') is not False for state in states)
            if ('full' in self.config['environment_tiers'] and automatic
                    and not set(wanted) <= set(config.get('services', []))):
                kind, selection = 'services', dict(services=wanted)
                evidence = dict(full_environment_available=True, capture_job_id=job['id'], report=report)
        if selection is None:
            return None
        return self.commands.environment_retry(Command(str(uuid.uuid4()), 'supervisor:requirements', job['unit_id'], job['unit_version']),
            job['id'], execution_fence=job['execution_fence'], input_manifest=job['input_manifest'],
            kind=kind, selection=selection, evidence=evidence)

    def cycle(self, *, after=None, limit=50):
        limit = _limit(limit)
        automatic = after is None
        with self.store.read_snapshot() as db:
            if automatic:
                row = db.execute("SELECT cursor FROM runtime_controls WHERE name='requirements_cursor'").fetchone()
                after = row[0] if row and row[0] else ''
            rows = db.execute("SELECT id FROM units WHERE id>? AND state NOT IN ('merged','closed','dissolved') ORDER BY id LIMIT ?", (after, limit + 1)).fetchall()
        items = []
        for row in rows[:limit]:
            try:
                items.append(self.observe(row['id']))
            except (GitHubUnavailable, TransitionError, StoreError, OSError, ValueError) as error:
                items.append({'unit_id': row['id'], 'error': getattr(error, 'code', type(error).__name__)})
        cursor = rows[limit-1]['id'] if len(rows) > limit else None
        if automatic:
            with self.store.write() as db:
                writable(db)
                db.execute("INSERT INTO runtime_controls(name,cursor,updated_at) VALUES('requirements_cursor',?,?) ON CONFLICT(name) DO UPDATE SET cursor=excluded.cursor,updated_at=excluded.updated_at", (cursor, self.clock()))
        return {'items': items, 'next_cursor': cursor}
