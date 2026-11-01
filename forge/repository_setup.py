"""Read-only repository access and external merge-rule observations."""
import threading
import time
import json

from .settings import digest
from .factory.github import GitHub, GitHubUnavailable
from .repository_policy import protection_ready

MAX_AGE = 300
CHECK_TIMEOUT = 120
DEPLOYMENT_LABEL = 'forge:deployment'


class SetupObservationChanged(ValueError):
    """The configuration owning a remote setup observation was replaced."""


def scope_matches(report, config, runtime, repo_id):
    from .app_access import shared_app
    from .factory.config import merge_check_scope
    return (not shared_app(config['connections']) or
            (report or {}).get('merge_check_scope') == merge_check_scope(runtime, repo_id))



def observation_key(config, row):
    return digest({'connections': config['connections'], 'repo_id': row['id'], 'name': row['name']})


class RepositorySetup:
    def __init__(self, settings, *, github_class=GitHub):
        self.settings = settings
        self.github_class = github_class
        # Deployment state belongs to the live factory, never to the setup copy.
        self.root = settings.authority.root
        self.database = settings.database

    def deployments(self):
        from .deployments import Deployments
        return Deployments(self.root, runner=None, github=None,
                           settings=self.settings, clock=self.settings.clock)

    def proposals(self):
        """A read for the UI: it never creates factory state."""
        from .store.operations import deployment_views
        return deployment_views(self.database)

    def read(self):
        with self.database.read_snapshot() as db:
            rows = db.execute('SELECT s.*,r.full_name FROM repository_setup_observations s JOIN repositories r ON r.id=s.repository_id').fetchall()
            return {'repositories': {str(row['repository_id']): dict(name=row['full_name'], status=row['status'],
                revision=row['source_revision'], started_at=row['started_at'], previous_status=row['previous_status'],
                report=json.loads(row['report_json']) if row['report_json'] else None,
                last_known=json.loads(row['last_known_json']) if row['last_known_json'] else None) for row in rows}}

    def save_observation(self, repo_id, data, *, expected_generation=None, expected_config=None):
        from .store.runtime import writable
        value = data['repositories'][str(repo_id)]
        with self.database.write() as db:
            writable(db)
            try:
                self.settings._generation(db, expected_generation)
            except ValueError as error:
                raise SetupObservationChanged(str(error)) from error
            if expected_config is not None:
                current = db.execute("SELECT settings_json FROM configuration WHERE domain=? AND subject='instance' AND active=1",
                                     (self.settings.domain,)).fetchone()
                if current is None or digest(json.loads(current[0])) != expected_config:
                    raise SetupObservationChanged('Repository settings changed during the protection check')
            # Access discovery can include a repository not yet enabled locally.
            db.execute('INSERT INTO repositories(id,full_name,enabled) VALUES(?,?,0) ON CONFLICT(id) DO NOTHING',
                       (repo_id, value['name']))
            db.execute('INSERT INTO repository_setup_observations VALUES(?,?,?,?,?,?,?,?) '
                'ON CONFLICT(repository_id) DO UPDATE SET status=excluded.status,source_revision=excluded.source_revision,'
                'started_at=excluded.started_at,previous_status=excluded.previous_status,report_json=excluded.report_json,'
                'last_known_json=excluded.last_known_json,source_fields_json=excluded.source_fields_json',
                (repo_id, value['status'], value['revision'], value['started_at'], value.get('previous_status'),
                 json.dumps(value.get('report'), allow_nan=False), json.dumps(value.get('last_known'), allow_nan=False),
                 json.dumps(sorted(value))))

    def registration(self):
        with self.database.read_snapshot() as db:
            row = db.execute("SELECT settings_json FROM setup_instances WHERE scope='live'").fetchone()
            return json.loads(row[0]) if row else {}

    def views(self, rows, config, *, runtime=None):
        from .repository_environments import RepositoryEnvironments
        environments = RepositoryEnvironments(self.settings)
        environment_snapshot = environments.snapshot(runtime=runtime)
        data, now = self.read(), self.settings.clock()
        profiles = {r['id']: r.get('deployment') for r in config['repositories']}
        denied = {r['id']: r.get('mirror_denied_repositories', []) for r in config['repositories']}
        from .factory.config import load as runtime_config, merge_mode, merge_check_scope
        runtime = runtime_config(self.settings.authority.root,scope=self.settings.scope) if runtime is None else runtime
        entries = self.proposals()
        result = []
        for row in rows:
            previous = data['repositories'].get(str(row['id']), {})
            current = previous.get('revision') == observation_key(config, row) and previous.get('name') == row['name']
            report = previous.get('report') if current else None
            if not scope_matches(report, config, runtime, row['id']):
                current, report = False, None
            fresh = report and 0 <= now - report.get('observed_at', 0) < MAX_AGE
            status = previous.get('status', 'unchecked') if current else 'unchecked'
            if status == 'checking' and now - previous.get('started_at', 0) >= CHECK_TIMEOUT:
                status = 'unknown'
            elif status != 'checking' and not fresh:
                status = 'stale' if report else 'unchecked'
            if status in ('ready', 'needs_setup'):
                status = 'ready' if protection_ready(report) else 'needs_setup'
            if not row.get('access_ready'):
                status = 'access_needed'
            entry, profile = entries.get(str(row['id'])) or {}, profiles.get(row['id'])
            result.append({**{key: value for key, value in row.items() if key != 'github_protection'}, 'mirror_denied_repositories': denied.get(row['id'], []), 'merge_policy':merge_mode(runtime,row['id']), 'protection_status': status, 'protection_ready': status == 'ready',
                           'merge_check_scope': merge_check_scope(runtime, row['id']), 'merge_check_revision': digest(runtime),
                           'environment': environments.view(row['id'], environment_snapshot),
                           'protection': report, 'protection_previous_status': previous.get('previous_status') if current else None,
                           'protection_last_known': previous.get('last_known') if current else None, 'rules_url': f"https://github.com/{row['name']}/settings/rules",
                           'setup_url': '/settings/repositories/' + str(row['id']) + '?setup=1',
                           'deployment': {'status': entry.get('status') or ('confirmed' if profile else 'idle'),
                                          'summary': entry.get('summary'), 'proposal': entry.get('proposal'),
                                          'profile': profile, 'observation': entry.get('observation'),
                                          'error': entry.get('error'), 'reason': entry.get('reason'),
                                          'exposure_issue': (profile or {}).get('exposure_issue') or entry.get('exposure_issue'),
                                          'reread_requested': bool(entry.get('reread_requested'))}})
        return result

    def view(self, repo_id=None):
        settings = self.settings.view()
        registration = self.registration()
        connections = {}
        for role, access in settings['roles'].items():
            app = registration.get('apps', {}).get(role, {})
            configured = settings['config']['connections'].get(role)
            same_app = not configured or configured.get('app_id') == app.get('connection', {}).get('app_id')
            connections[role] = {**access, 'install_url': app.get('install_url') if same_app else None}
        rows = settings['repositories']
        if repo_id is not None:
            rows = [r for r in rows if r['id'] == repo_id]
            if not rows:
                raise ValueError('Refresh GitHub access to find this repository')
        return {'revision': settings['revision'], 'repositories': rows,
                'connections': connections, 'sync': settings['sync'],
                'activation_available': self.settings.scope != 'setup',
                'continue_url': '/onboarding' if self.settings.scope == 'setup' else '/factory'}

    def merge_policy(self, repo_id, revision, mode):
        from .factory.config import load, save
        if type(repo_id) is not int or repo_id <= 0 or mode not in ('auto', 'human'):
            raise ValueError('Select a repository and auto or human merge policy')
        if not any(repo['id'] == repo_id for repo in self.settings.load()['repositories']):
            raise ValueError('Configure this repository before changing its merge policy')
        runtime = load(self.settings.authority.root, scope=self.settings.scope)
        if revision != digest(runtime):
            raise ValueError('Runtime settings changed. Reload before saving merge policy')
        runtime['repository_merge_policies'][str(repo_id)] = mode
        save(self.settings.authority.root, runtime, revision=revision, scope=self.settings.scope)
        return self.view(repo_id)

    def review_scope(self, repo_id, revision, scope):
        from .factory.config import load, save
        if type(repo_id) is not int or repo_id <= 0 or scope not in ('all', 'forge'):
            raise ValueError('Select a repository and all or forge review scope')
        if not any(repo['id'] == repo_id for repo in self.settings.load()['repositories']):
            raise ValueError('Configure this repository before changing its review scope')
        runtime = load(self.settings.authority.root, scope=self.settings.scope)
        if revision != digest(runtime):
            raise ValueError('Runtime settings changed. Reload before saving review scope')
        runtime['repository_merge_check_scopes'][str(repo_id)] = scope
        save(self.settings.authority.root, runtime, revision=revision, scope=self.settings.scope)
        return self.view(repo_id)

    def mirror_policy(self, repo_id, revision, denied_repositories):
        from .settings import mirror_denied_repositories
        if type(repo_id) is not int or repo_id <= 0:
            raise ValueError('Expected a numeric repository identity')
        entries = mirror_denied_repositories(denied_repositories)
        config = self.settings.load()
        if revision != digest(config):
            raise ValueError('Repository settings changed. Reload before saving.')
        configured = next((r for r in config['repositories'] if r['id'] == repo_id), None)
        if configured is None:
            raise ValueError('Configure this repository before changing its mirror policy')
        # No remote permission changes. IDs supplied with existing restrictions
        # remain explicit in the form so renames cannot silently drop them.
        configured['mirror_denied_repositories'] = [dict(entry, canonical_name=entry['canonical_name'].lower()) for entry in entries]
        self.settings.save(config, revision)
        return self.view(repo_id)

    def enable(self, repo_id, revision):
        if self.settings.scope == 'setup':
            raise ValueError('Finish initial Forge setup before enabling automation')
        config = self.settings.load()
        if revision != digest(config):
            raise ValueError('Repository settings changed. Check again before enabling.')
        row = self.view(repo_id)['repositories'][0]
        configured = next((r for r in config['repositories'] if r['id'] == repo_id), None)
        if configured is None:
            config['repositories'].append({'id': row['id'], 'name': row['name'], 'enabled': True})
        else:
            configured['enabled'] = True
        self.settings.save(config, revision)
        return self.view(repo_id)

    def disable(self, repo_id, revision):
        if self.settings.scope == 'setup':
            raise ValueError('Finish initial Forge setup before changing automation')
        config = self.settings.load()
        if revision != digest(config):
            raise ValueError('Repository settings changed. Reload before disabling.')
        row = self.view(repo_id)['repositories'][0]
        configured = next((r for r in config['repositories'] if r['id'] == repo_id), None)
        if configured is None:
            config['repositories'].append({'id': row['id'], 'name': row['name'], 'enabled': False})
        else:
            configured['enabled'] = False
        # save checks the revision again under the config lock. Access and rule
        # readiness are only required when enabling, so lost access cannot
        # prevent an operator from stopping new work.
        self.settings.save(config, revision)
        return self.view(repo_id)

    def deployment(self, repo_id, revision, action, domain=None, reason=None):
        """Confirming is the only moment settings change or an issue is filed."""
        if self.settings.scope == 'setup':
            raise ValueError('Finish initial Forge setup before confirming a deployment profile')
        config = self.settings.load()
        if revision != digest(config):
            raise ValueError('Repository settings changed. Reload and check again.')
        row = self.view(repo_id)['repositories'][0]
        store = self.deployments()
        if action == 'reread':
            store.request_reread(repo_id, row['name'])
            return self.view(repo_id)
        if action == 'reject':
            store.reject(repo_id, reason)
            return self.view(repo_id)
        if action != 'confirm':
            raise ValueError('A deployment decision is confirm, reject, or reread')
        configured = next((r for r in config['repositories'] if r['id'] == repo_id), None)
        if configured is None or not configured['enabled']:
            raise ValueError('Enable this repository before confirming its deployment profile.')
        entry = store.read()['repositories'].get(str(repo_id)) or {}
        proposed = entry.get('proposal') or {}
        profile = store.confirm(repo_id, domain)
        # A second read of the same repository must never file the issue twice: the
        # number survives in the store even when settings could not record it.
        profile['exposure_issue'] = (configured.get('deployment') or {}).get('exposure_issue') or entry.get('exposure_issue')
        configured['deployment'] = profile
        try:
            self.settings.save(config, revision)
        except (ValueError, OSError):
            try:
                store.save(repo_id, status='proposed')  # A decision never outlives its write.
            except Exception:
                pass  # The operator needs the refusal, not the rollback's own failure.
            raise
        issue = proposed.get('proposal')
        if issue and profile['exposure_issue'] is None and profile['exposure']['kind'] == 'none':
            try:
                github = self.github_class(self.settings, {}, clock=self.settings.clock)
                github.deadline = time.monotonic() + 60
                # The ready label is the operator's to add; Forge only labels the subject.
                number = github.file_issue({'repo': row['name'], 'repo_id': repo_id},
                                           issue['title'], issue['body'], [DEPLOYMENT_LABEL])
            except (GitHubUnavailable, ValueError, OSError) as error:
                store.save(repo_id, error=str(error) if isinstance(error, GitHubUnavailable) else
                           'The deployment issue could not be filed; re-read deployment to retry')
            else:
                # Record the number before anything else can fail: a lost number
                # would let the next confirmation file the same issue again.
                store.save(repo_id, exposure_issue=number)
                try:
                    config = self.settings.load()
                    current = digest(config)
                    saved = next((r for r in config['repositories'] if r['id'] == repo_id), None)
                    if saved and saved.get('deployment'):
                        saved['deployment']['exposure_issue'] = number
                        self.settings.save(config, current)
                except (ValueError, OSError) as error:
                    store.save(repo_id, error='Deployment issue #' + str(number) +
                               ' was filed but not recorded in settings: ' + str(error))
        return self.view(repo_id)

    def check(self, repo_id=None, revision=None):
        try:
            with self.settings.locked('repository-check', blocking=False):
                if self.settings.paused():
                    return False
                config = self.settings.load()
                generation = self.settings.generation()
                current_revision = digest(config)
                if revision is not None and revision != current_revision:
                    raise ValueError('Repository settings changed. Reload and check again.')
                view = self.settings.view()
                rows = [r for r in view['repositories'] if r['id'] == repo_id] if repo_id is not None else [r for r in view['repositories'] if r['enabled']]
                if repo_id is not None and not rows:
                    raise ValueError('Refresh GitHub access to find this repository')
                data = self.read()
                # Sequential bounded checks: a failed repository never aborts the batch.
                for row in rows:
                    if self.settings.paused() or digest(self.settings.load()) != current_revision:
                        return False
                    prior = data['repositories'].get(str(row['id']), {})
                    same = prior.get('revision') == observation_key(config, row)
                    known = (prior.get('report') if (prior.get('report') or {}).get('available') else prior.get('last_known')) if same else None
                    # A last-known success is display history, not current
                    # admission evidence after a completed failed check.
                    data['repositories'][str(row['id'])] = {'report': prior.get('report') if same else None, 'last_known': known,
                        'previous_status': ('ready' if protection_ready(known) else 'needs_setup') if known else None, 'revision': observation_key(config, row), 'name': row['name'],
                        'status': 'checking', 'started_at': self.settings.clock()}
                    self.save_observation(row['id'], data, expected_generation=generation)
                    report = {'repo_id': row['id'], 'branch': None, 'available': False,
                              'observed_at': self.settings.clock(), 'requirements': [], 'errors': []}
                    try:
                        if not row['access_ready']:
                            raise ValueError('The Forge App needs access. Refresh GitHub access first.')
                        from .factory.config import load as runtime_config
                        github = self.github_class(self.settings, runtime_config(self.settings.authority.root,scope=self.settings.scope), clock=self.settings.clock)
                        github.deadline = time.monotonic() + 60
                        item = {'repo_id': row['id'], 'repo': row['name']}
                        branch = github.target(item)
                        report = github.protection_report(item, {'base_ref': branch})
                    except (GitHubUnavailable, ValueError, OSError):
                        report['errors'] = [{'detail': 'Could not check this repository. Refresh GitHub access and try again.'}]
                    report['observed_at'] = self.settings.clock()
                    status = ('unknown' if not report['available'] else
                              'ready' if protection_ready(report) else 'needs_setup')
                    data['repositories'][str(row['id'])].update(status=status, report=report)
                    self.save_observation(row['id'], data, expected_generation=generation)
                return True
        except BlockingIOError:
            return False

    def start_check(self, repo_id, revision):
        if revision != digest(self.settings.load()):
            raise ValueError('Repository settings changed. Reload and check again.')
        self.view(repo_id)  # Validate identity before acknowledging the request.
        def run():
            try:
                self.check(repo_id, revision)
            except (ValueError, OSError):
                pass  # A concurrent config/reset invalidates the old operation.
        threading.Thread(target=run, daemon=True).start()
