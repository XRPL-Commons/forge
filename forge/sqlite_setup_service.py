"""Selected SQLite setup driver. Reset retains history and verified ownership."""
import json
import time
import uuid

from .setup_service import Service
from .settings import Settings, write_json
from .factory.config import load
from .factory.control import controller_lock
from .factory.runner import RunnerCapacity, RunnerError
from .store.files import StateError
from .sqlite_identity import identity_effect_lock, audit_in
from .sqlite_onboarding import control_in
from .store.runtime import writable
from .store.auxiliary import active, save_in
from .store.models import Command
from .store.transitions import Lifecycle


class SqliteService(Service):
    @property
    def database(self):
        return self.setup.database

    def drain(self, scope):
        from .factory.sqlite_runtime import cycle
        config = load(self.root)
        if scope:
            identifiers = [e['id'] for e in config['provider_pool']]
            ready = identifiers if config['provider_ready'] is None else config['provider_ready']
            config['provider_ready'] = [key for key in ready if key not in scope]
        cycle(self.root, config=config, runner=self.runner(config), github=self.github_class(Settings(self.root), config), dispatch=bool(scope))
        with self.database.read_snapshot() as db:
            rows = db.execute('SELECT l.runner_config_json FROM job_leases lease JOIN job_launches l ON l.job_id=lease.job_id LIMIT 101').fetchall()
        references = {e['provider_auth_file_env'] for e in config['provider_pool']
                      if e['id'] in (scope or []) and e.get('provider_auth_file_env')}
        return bool(rows) if not scope else any(json.loads(row[0]).get('provider_auth_file_env') in references for row in rows)

    def start(self, data):
        op = data['operation']
        with identity_effect_lock(self.database), controller_lock(self.root):
            if self.setup.revision(data) != op['revision'] or not self.setup.qualified(data):
                raise ValueError('Setup changed or verification expired; run readiness checks again')
            config = self.access(data)
            self.runner(config).ready()
            if Settings(self.root).paused():
                raise ValueError('Reset is in progress; finish it before starting')
            current = self.setup.read()
            if self.setup.revision(current) != op['revision'] or (current.get('operation') or {}).get('id') != op['id']:
                raise StateError('Setup changed during verification')
            settings = Settings(self.root)
            settings.save_config(self.setup.settings().load())
            settings.save_observation(self.setup.settings().observation())
            config.update(enabled=True, merge_enabled=True)
            self.setup.save_runtime(config, active=True)
            self.update(op, phase='active', operation=None, error=None,
                        activation_request=op['id'], activation_revision=op['revision'])

    def _pause_units(self, op):
        with self.database.read_snapshot() as db:
            rows = db.execute("SELECT id,version FROM units u WHERE state NOT IN ('merged','closed','dissolved') "
                              "AND NOT EXISTS (SELECT 1 FROM unit_gates g WHERE g.unit_id=u.id AND g.code='operator_pause' AND g.resolved_at IS NULL) "
                              'ORDER BY id LIMIT 50').fetchall()
        for row in rows:
            Lifecycle(self.database).pause(Command('setup-reset:' + op['id'] + ':' + row['id'], 'setup:reset', row['id'], row['version']))
        return len(rows)

    def reset(self, data):
        from .factory.sqlite_runtime import cleanup
        op = data['operation']
        with identity_effect_lock(self.database), controller_lock(self.root):
            current = self.setup.read()
            if current['generation'] != op['generation'] or (current.get('operation') or {}).get('id') != op['id']:
                raise StateError('Reset belongs to an earlier setup generation')
            if self._pause_units(op):
                return
            config = load(self.root)
            pending = cleanup(self.root, runner=self.runner(config), github=self.github_class(Settings(self.root), config))['pending']
            for job in active(self.database):
                domain, owner = self._domain(job['id'])
                job['fenced'] = True
                with self.database.write() as db:
                    save_in(db, domain, owner, job, self.setup.clock())
                self.runner(job['runner_config']).reset(job)
                job['cleanup'] = True
                with self.database.write() as db:
                    save_in(db, domain, owner, job, self.setup.clock())
            with self.database.read_snapshot() as db:
                unresolved = db.execute("SELECT 1 FROM external_effects WHERE status IN ('pending','running','uncertain') LIMIT 1").fetchone()
                leases = db.execute('SELECT 1 FROM job_leases UNION ALL SELECT 1 FROM auxiliary_leases UNION ALL SELECT 1 FROM discussion_leases LIMIT 1').fetchone()
            if pending or leases:
                return
            if unresolved:
                self.update(op, error='Reset is waiting for GitHub effect reconciliation; history and credentials are retained.')
                return
            from .harnesses.accounts import reset_logins
            reset_logins(self.root)
            credentials = self.setup.credentials
            with credentials.locked():
                write_json(credentials.root / 'index.json', {})
                credentials._prune({})
                for pending in credentials.root.glob('pending-login-*.json'):
                    pending.unlink()
            now = self.setup.clock()
            # One commit retires operational config and grants, preserving identity,
            # lifecycle history, artifacts, and the selected database itself.
            with self.database.write() as db:
                writable(db)
                db.execute("UPDATE configuration SET active=0 WHERE active=1 AND domain IN ('factory.json','runtime.json','setup/factory.json','setup/runtime.json')")
                db.execute('UPDATE repositories SET enabled=0')
                db.execute("UPDATE environment_scans SET status='failed',error='Setup was reset; scan requirements again.' WHERE status='running'")
                db.execute("UPDATE environment_proposals SET status='superseded' WHERE status IN ('proposed','approved','applied')")
                db.execute("UPDATE client_connections SET revoked_at=?,revocation_reason='setup_reset' WHERE revoked_at IS NULL", (now,))
                db.execute("UPDATE client_pairings SET status='denied' WHERE status IN ('pending','approved')")
                control_in(db, 'factory_active', False, now)
                # Keep the reset fence until the new generation is durably saved.
                audit_in(db, self.setup.clock, 'setup_reset_retired', request_id=op['id'])
            jobs = []
            self.update(op, generation=str(uuid.uuid4()), phase='setup', operation=None, apps={}, checks={}, repository_check=None,
                        probe_jobs=jobs, selected=None, provider_revision=None, provider_checks={}, provider_errors={},
                        probe_plan=[], probe_ids=[], readiness=None, qualification=None, error=None, owner='', account_type=None,
                        challenge=None, activation_request=None, activation_revision=None,
                        reset_receipt={'id': op['id'], 'generation': op['generation'], 'at': now})
            with self.database.write() as db:
                control_in(db, 'factory_paused', False, now)

    def _domain(self, identifier):
        with self.database.read_snapshot() as db:
            row = db.execute('SELECT domain,owner_id FROM auxiliary_executions WHERE id=?', (identifier,)).fetchone()
            return tuple(row)

    def tick(self):
        from .factory.discussions import Discussions
        from .factory.sqlite_runtime import cycle
        with self.database.write() as db:
            writable(db)
            now = self.setup.clock()
            db.execute("INSERT INTO runtime_controls(name,heartbeat_at,updated_at,details_json) VALUES('setup_service',?,?,'{}') "
                       'ON CONFLICT(name) DO UPDATE SET heartbeat_at=excluded.heartbeat_at,updated_at=excluded.updated_at', (now, now))
        data = self.setup.read()
        op = data.get('operation')
        scoped = bool(op and op['action'] == 'harness_check' and op.get('providers') and data['phase'] == 'active')
        discussions = Discussions(self.root, runner=self.runner, boot=self.boot)
        if op:
            with controller_lock(self.root):
                discussions.cancel_all()
                jobs = discussions.active_jobs()
                if jobs:
                    discussions.drive(jobs[0], None)
                    return
            try:
                if op['action'] == 'reset':
                    self.reset(data)
                elif op['action'] == 'start':
                    self.start(data)
                else:
                    with identity_effect_lock(self.database):
                        self.check(data)
            except RunnerCapacity:
                if data.get('probe_jobs'):
                    data['probe_jobs'][-1].setdefault('capacity_wait_since', time.time())
                    self.update(op, probe_jobs=data['probe_jobs'])
            except (OSError, ValueError, RuntimeError) as error:
                message = str(error) if isinstance(error, (ValueError, RunnerError, StateError)) else 'Setup operation failed; retry after checking service health'
                try:
                    changes = {} if op['action'] == 'reset' else {'operation': None}
                    latest = self.setup.read()
                    if op['action'] in ('check', 'harness_check') and any(not j['cleanup'] for j in latest['probe_jobs']):
                        for job in latest['probe_jobs']:
                            job['fenced'] = True
                        changes = {'probe_jobs': latest['probe_jobs'], 'probe_error': message}
                    self.update(op, error=message, **changes)
                except StateError:
                    pass
            if not scoped:
                return
        settings, config = Settings(self.root), load(self.root)
        if scoped:
            identifiers = [e['id'] for e in config['provider_pool']]
            ready = identifiers if config['provider_ready'] is None else config['provider_ready']
            config['provider_ready'] = [key for key in ready if key not in op['providers']]
        if settings.paused():
            if data['phase'] == 'setup' and data.get('reset_receipt'):
                with self.database.write() as db:
                    fence = db.execute("SELECT details_json FROM runtime_controls WHERE name='factory_paused'").fetchone()
                    if fence and json.loads(fence[0]).get('generation') != data['generation']:
                        control_in(db, 'factory_paused', False, self.setup.clock())
            return
        if data['phase'] != 'active':
            return
        if time.monotonic() < self.next_cycle:
            if not op:
                self.converse(discussions)
            return
        with controller_lock(self.root):
            if time.monotonic() >= self.next_sync:
                settings.start_refresh()
                self.next_sync = time.monotonic() + settings.load()['sync_interval_seconds']
            cycle(self.root, config=config, runner=self.runner(config), github=self.github_class(settings, config), settings=settings)
            if not op:
                self.auxiliary_cycles(discussions, settings, config)
            self.next_cycle = time.monotonic() + config['cycle_seconds']
            self.next_discussion = time.monotonic() + 4

    def auxiliary_cycles(self, discussions, settings, config):
        from .deployments import Deployments
        from .factory.github import GitHubUnavailable
        github = self.github_class(settings, config)
        github.deadline = time.monotonic() + config['cycle_budget_seconds'] / 4
        deployments = Deployments(self.root, runner=self.runner, github=github, settings=settings)
        try:
            deployments.tick(config)
        except (RunnerError, GitHubUnavailable, ValueError, StateError, OSError) as error:
            deployments.fail(str(error))
        github = self.github_class(settings, config)
        github.deadline = time.monotonic() + 10
        try:
            discussions.tick(config, github, settings)
        except (RunnerError, GitHubUnavailable, ValueError, StateError, OSError):
            pass
