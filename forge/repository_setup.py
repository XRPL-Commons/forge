"""Read-only repository preparation. The operator imports generated rules on GitHub."""
import threading
import time

from .settings import digest, read_json, write_json
from .factory.github import GitHub, GitHubUnavailable

MAX_AGE = 300
RULESET_NAME = 'Forge review baseline'


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

    def read(self):
        try:
            value = read_json(self.path)
            return value if isinstance(value, dict) and isinstance(value.get('repositories'), dict) else {'repositories': {}}
        except (OSError, ValueError):
            return {'repositories': {}}

    def views(self, rows, config):
        data, now = self.read(), self.settings.clock()
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
            result.append({**row, 'protection_status': status, 'protection_ready': status == 'ready',
                           'protection': report, 'rules_url': f"https://github.com/{row['name']}/settings/rules",
                           'setup_url': '/settings/repository?repo_id=' + str(row['id'])})
        return result

    def view(self, repo_id=None):
        settings = self.settings.view()
        root = self.settings.root.parent if self.settings.root.name == 'setup' else self.settings.root
        registration = read_json(root / 'state/onboarding/setup.json') if (root / 'state/onboarding/setup.json').exists() else {}
        connections = {}
        for role, access in settings['roles'].items():
            app = registration.get('apps', {}).get(role, {})
            connections[role] = {**access, 'install_url': app.get('install_url')}
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
                    data['repositories'][str(row['id'])] = {'revision': observation_key(config, row), 'name': row['name'],
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
