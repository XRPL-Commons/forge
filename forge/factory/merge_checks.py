"""Admit every in-scope pull request to the same durable Review/Audit lifecycle."""
import time

from ..store.models import TransitionError
from ..store.review_pulls import ReviewPullRequests, normalize_source
from ..store.transitions import manifest_hash
from .config import merge_check_scope, policy
from .github import GitHubUnavailable

MERGE_POLICY = 'forge/merge-policy'


def merge_policy_rule(app_id):
    if type(app_id) is not int or app_id <= 0:
        raise ValueError('The Forge App ID is required to pin forge/merge-policy')
    return {'type': 'required_status_checks', 'parameters': {
        'strict_required_status_checks_policy': False,
        'required_status_checks': [{'context': MERGE_POLICY, 'integration_id': app_id}]}}


class ReviewPullsController:
    """GitHub reads outside SQL; store admission never publishes a passing check."""
    def __init__(self, store, github, config, settings=None, *, clock=time.time):
        self.store, self.github, self.config, self.settings, self.clock = store, github, config, settings, clock
        self.commands = ReviewPullRequests(store, clock=clock)

    def _config(self):
        return self.config() if callable(self.config) else self.config

    def _observe(self, item, remote, *, expected_version):
        from ..install_scope import pending
        if pending(self.store):
            return {'admitted': False, 'reason': 'installation_smoke'}
        config = self._config()
        source = normalize_source(remote, item['repo_id'], item['repo'])
        enabled = bool(config['enabled'])
        with self.store.read_snapshot() as db:
            paused = db.execute("SELECT enabled FROM runtime_controls WHERE name='factory_paused'").fetchone()
        if paused and paused[0]:
            return {'pull_number': source['pull_number'], 'admitted': False, 'reason': 'factory_paused'}
        return self.commands.observe(source, policy_hash=manifest_hash(policy(config, item['repo_id'], repository_name=item['repo'])),
            scope=merge_check_scope(config, item['repo_id']), enabled=enabled, hold_labels=config['hold_labels'], expected_version=expected_version)

    def reconcile(self, item, pulls):
        """Discover candidates from a complete inventory; bound units refresh in cycle."""
        from ..install_scope import pending
        if pending(self.store):
            return []
        config = self._config()
        if merge_check_scope(config, item['repo_id']) != 'all' or not config['enabled']:
            return []
        with self.store.read_snapshot() as db:
            known = {row[0] for row in db.execute("SELECT p.pull_number FROM review_pull_requests p JOIN units u ON u.id=p.unit_id WHERE p.repository_id=? AND u.state NOT IN ('closed','merged')", (item['repo_id'],))}
            owned = {row[0] for row in db.execute('SELECT c.pull_number FROM candidates c JOIN units u ON u.id=c.unit_id '
                'WHERE u.repository_id=? AND NOT EXISTS(SELECT 1 FROM review_pull_requests p WHERE p.unit_id=u.id)', (item['repo_id'],))}
        results = []
        for pr in pulls:
            if pr.get('forge_owned') or pr.get('number') in known | owned or pr.get('state') != 'open' or pr.get('draft') is not False:
                continue
            # Listing is discovery only. Fetch the exact PR for complete admission.
            remote = self.github.pr(item, pr['number'])
            if remote.get('number') != pr['number']:
                raise GitHubUnavailable('Review PR identity changed during observation')
            results.append(self._observe(item, remote, expected_version=0))
            if len(results) >= 50:
                break
        return results

    def cycle(self, *, after=None, limit=50):
        """Refresh existing review bindings once per caller-owned bounded sweep."""
        from ..store.queries import _limit
        limit = _limit(limit)
        with self.store.read_snapshot() as db:
            rows = [dict(row) for row in db.execute('SELECT p.unit_id,p.pull_number,u.version,r.id AS repo_id,r.full_name AS repo '
                'FROM review_pull_requests p JOIN units u ON u.id=p.unit_id JOIN repositories r ON r.id=p.repository_id '
                "WHERE u.state NOT IN ('closed','merged') AND p.unit_id>? ORDER BY p.unit_id LIMIT ?", (after or '', limit + 1))]
        items = []
        for row in rows[:limit]:
            try:
                remote = self.github.pr(row, row['pull_number'])
                if remote.get('number') != row['pull_number']:
                    raise GitHubUnavailable('Review PR identity changed during observation')
                items.append(self._observe(row, remote, expected_version=row['version']))
            except GitHubUnavailable as error:
                items.append({'unit_id': row['unit_id'], 'reason': 'github_unavailable', 'http_status': error.status})
            except TransitionError:
                items.append({'unit_id': row['unit_id'], 'reason': 'observation_unavailable'})
        return {'items': items, 'next_cursor': rows[limit - 1]['unit_id'] if len(rows) > limit else None}
