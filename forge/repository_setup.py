"""Read-only repository preparation. The operator imports generated rules on GitHub."""
import threading
import time

from .settings import digest, read_json, write_json
from .state import StateError
from .factory.github import GitHub, GitHubUnavailable

MAX_AGE = 300
RULESET_NAME = 'Forge review baseline'
DEPLOYMENT_LABEL = 'forge:deployment'


def ruleset(branch):
    if not isinstance(branch, str) or not branch or any(c in branch for c in '\r\n\x00'):
        raise ValueError('Verify the repository branch before downloading rules')
    return {'name': RULESET_NAME, 'target': 'branch', 'enforcement': 'active', 'bypass_actors': [],
            'conditions': {'ref_name': {'include': ['refs/heads/' + branch], 'exclude': []}},
            'rules': [{'type': 'pull_request', 'parameters': {
                'required_approving_review_count': 2, 'dismiss_stale_reviews_on_push': True,
                'require_code_owner_review': False, 'require_last_push_approval': False,
                'required_review_thread_resolution': False}}]}


def observation_key(config, row):
    return digest({'connections': config['connections'], 'repo_id': row['id'], 'name': row['name']})


class RepositorySetup:
    def __init__(self, settings, *, github_class=GitHub):
        self.settings = settings
        self.github_class = github_class
        self.path = settings.state / 'repositories.json'
        # Deployment state belongs to the live factory, never to the setup copy.
        self.root = settings.root.parent if settings.root.name == 'setup' else settings.root

    def deployments(self):
        from .deployments import Deployments
        return Deployments(self.root, runner=None, github=None,
                           settings=self.settings, clock=self.settings.clock)

    def proposals(self):
        """The operator view of the store never fails closed; it shows what it has."""
        try:
            return self.deployments().read()['repositories']
        except (StateError, OSError, ValueError):
            return {}

    def read(self):
        try:
            value = read_json(self.path)
            return value if isinstance(value, dict) and isinstance(value.get('repositories'), dict) else {'repositories': {}}
        except (OSError, ValueError):
            return {'repositories': {}}

    def views(self, rows, config):
        data, now = self.read(), self.settings.clock()
        profiles = {r['id']: r.get('deployment') for r in config['repositories']}
        entries = self.proposals()
        result = []
        for row in rows:
            previous = data['repositories'].get(str(row['id']), {})
            current = previous.get('revision') == observation_key(config, row) and previous.get('name') == row['name']
            report = previous.get('report') if current else None
            fresh = report and 0 <= now - report.get('observed_at', 0) < MAX_AGE
            status = previous.get('status', 'unchecked') if current else 'unchecked'
            if status == 'checking' and now - previous.get('started_at', 0) > 120:
                status = 'unknown'
            elif status != 'checking' and not fresh:
                status = 'stale' if report else 'unchecked'
            if not row.get('access_ready'):
                status = 'access_needed'
            entry, profile = entries.get(str(row['id'])) or {}, profiles.get(row['id'])
            result.append({**row, 'protection_status': status, 'protection_ready': status == 'ready',
                           'protection': report, 'protection_previous_status': previous.get('previous_status') if current else None,
                           'protection_last_known': previous.get('last_known') if current else None, 'rules_url': f"https://github.com/{row['name']}/settings/rules",
                           'setup_url': '/settings/repository?repo_id=' + str(row['id']),
                           'deployment': {'status': entry.get('status') or ('confirmed' if profile else 'idle'),
                                          'summary': entry.get('summary'), 'proposal': entry.get('proposal'),
                                          'profile': profile, 'observation': entry.get('observation'),
                                          'error': entry.get('error'), 'reason': entry.get('reason'),
                                          'exposure_issue': (profile or {}).get('exposure_issue'),
                                          'reread_requested': bool(entry.get('reread_requested'))}})
        return result

    def view(self, repo_id=None):
        settings = self.settings.view()
        registration = read_json(self.root / 'state/onboarding/setup.json') if (self.root / 'state/onboarding/setup.json').exists() else {}
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
                'activation_available': self.settings.root.name != 'setup',
                'continue_url': '/onboarding' if self.settings.root.name == 'setup' else '/factory'}

    def enable(self, repo_id, revision):
        if self.settings.root.name == 'setup':
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

    def deployment(self, repo_id, revision, action, domain=None, reason=None):
        """Confirming is the only moment settings change or an issue is filed."""
        if self.settings.root.name == 'setup':
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
        if configured is None:
            raise ValueError('Enable this repository before confirming its deployment profile.')
        proposed = (store.read()['repositories'].get(str(repo_id)) or {}).get('proposal') or {}
        profile = store.confirm(repo_id, domain)
        # A second read of the same repository must never file the issue twice.
        profile['exposure_issue'] = (configured.get('deployment') or {}).get('exposure_issue')
        configured['deployment'] = profile
        try:
            self.settings.save(config, revision)
        except (ValueError, OSError):
            store.save(repo_id, status='proposed')  # A decision never outlives its write.
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
                config = self.settings.load()
                current = digest(config)
                saved = next((r for r in config['repositories'] if r['id'] == repo_id), None)
                if saved and saved.get('deployment'):
                    saved['deployment']['exposure_issue'] = number
                    self.settings.save(config, current)
        return self.view(repo_id)

    def check(self, repo_id=None, revision=None):
        try:
            with self.settings.locked('repository-check', blocking=False):
                if self.settings.paused():
                    return False
                config = self.settings.load()
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
                    data['repositories'][str(row['id'])] = {'report': known, 'last_known': known,
                        'previous_status': ('ready' if known.get('requirements') and all(r['passed'] for r in known['requirements']) else 'needs_setup') if known else None, 'revision': observation_key(config, row), 'name': row['name'],
                        'status': 'checking', 'started_at': self.settings.clock()}
                    write_json(self.path, data)
                    report = {'repo_id': row['id'], 'branch': None, 'available': False,
                              'observed_at': self.settings.clock(), 'requirements': [], 'errors': []}
                    try:
                        if not row['access_ready']:
                            raise ValueError('All four Apps need access. Refresh GitHub access first.')
                        github = self.github_class(self.settings, {}, clock=self.settings.clock)
                        github.deadline = time.monotonic() + 60
                        item = {'repo_id': row['id'], 'repo': row['name']}
                        branch = github.target(item)
                        report = github.protection_report(item, {'base_ref': branch})
                        # Inventory is for a helpful edit link, never proof of enforcement.
                        try:
                            inventory = github.pages('controller', item, '/rulesets?includes_parents=true')
                            managed = [r for r in inventory if r.get('name') == RULESET_NAME and r.get('source_type') == 'Repository' and type(r.get('id')) is int]
                            if len(managed) == 1:
                                report['edit_url'] = f"https://github.com/{row['name']}/settings/rules/{managed[0]['id']}"
                        except GitHubUnavailable:
                            pass
                    except (GitHubUnavailable, ValueError, OSError):
                        report['errors'] = [{'detail': 'Could not check this repository. Refresh GitHub access and try again.'}]
                    report['observed_at'] = self.settings.clock()
                    status = ('unknown' if not report['available'] else
                              'ready' if report['requirements'] and all(r['passed'] for r in report['requirements']) else 'needs_setup')
                    data['repositories'][str(row['id'])].update(status=status, report=report)
                    write_json(self.path, data)
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

    def download(self, repo_id, revision):
        view = self.view(repo_id)
        if view['revision'] != revision:
            raise ValueError('Repository settings changed. Check again before downloading.')
        row = view['repositories'][0]
        report = row.get('protection') or {}
        if row['protection_status'] not in ('ready', 'needs_setup') or not report.get('branch'):
            raise ValueError('Check this repository before downloading its rules.')
        return ruleset(report['branch']), 'forge-review-rules-' + str(repo_id) + '.json'
