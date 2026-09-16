"""Role-scoped GitHub effects and authoritative evidence, never terminal inference."""
import json
import base64
from datetime import datetime
import time
from urllib.parse import quote

from ..app_access import AppClient, AccessError, ROLE_PERMISSIONS
from .contracts import candidate, digest, submission


class GitHubUnavailable(RuntimeError):
    def __init__(self, message, *, status=None):
        super().__init__(message)
        self.status = status


class GitHub:
    def __init__(self, settings, runtime, *, client=None, clock=time.time):
        self.settings, self.runtime = settings, runtime
        self.client, self.clock = client or settings.client, clock
        self.tokens = {}

    def connection(self, role, repo_id=None):
        role = 'controller' if role == 'recovery' else role
        connection = self.settings.load()['connections'][role]
        if 'installation_ids' in connection and repo_id is not None:
            observation = self.settings.observation()['roles'].get(role, {})
            if observation.get('connection') != digest(connection):
                raise GitHubUnavailable('Installation mapping needs a fresh access sync')
            matches = [r for r in observation.get('repositories', []) if r['id'] == repo_id]
            if len(matches) != 1 or matches[0].get('installation_id') not in connection['installation_ids']:
                raise GitHubUnavailable('Repository installation mapping is unverified')
            return {'app_id': connection['app_id'], 'private_key_env': connection['private_key_env'],
                    'installation_id': matches[0]['installation_id']}
        return connection

    def token(self, role, repo_id, *, worker=False):
        connection = self.connection(role, repo_id)
        key = (role, repo_id, worker, digest(connection))
        cached = self.tokens.get(key)
        if not worker and cached and cached[1] > self.clock():
            return cached[0]
        permissions = ({'metadata': 'read', 'contents': 'read', 'issues': 'read', 'pull_requests': 'read'}
                       if role == 'recovery' else dict(ROLE_PERMISSIONS[role]))
        if worker and role in ('review', 'audit'):
            # Worker findings are published by the supervisor after validation.
            permissions = {'metadata': 'read', 'contents': 'read', 'issues': 'read', 'pull_requests': 'read'}
        if role == 'controller' and not worker:
            permissions['administration'] = 'read'
        try:
            value = self.client.request('POST', f"/app/installations/{connection['installation_id']}/access_tokens",
                                        self.client.jwt(connection), {'repository_ids': [repo_id], 'permissions': permissions})
            token = value['token']
            if not isinstance(token, str) or not token:
                raise ValueError()
        except (AccessError, KeyError, ValueError):
            raise GitHubUnavailable('Role credential could not be issued for this repository') from None
        self.tokens[key] = (token, self.clock() + 3000)
        return token

    def call(self, role, repo, method, suffix, payload=None):
        if time.monotonic() >= getattr(self, 'deadline', float('inf')):
            raise GitHubUnavailable('Cycle observation budget reached; resume next cycle')
        try:
            return self.client.request(method, f"/repos/{repo['repo']}" + suffix,
                                       self.token(role, repo['repo_id']), payload)
        except AccessError as error:
            raise GitHubUnavailable('GitHub observation or publication is unconfirmed', status=error.status) from None

    def pages(self, role, repo, suffix, key=None):
        rows = []
        for page in range(1, 101):
            sep = '&' if '?' in suffix else '?'
            data = self.call(role, repo, 'GET', suffix + f'{sep}per_page=100&page={page}')
            batch = data.get(key) if key and isinstance(data, dict) else data
            if not isinstance(batch, list):
                raise GitHubUnavailable('GitHub inventory is malformed')
            rows.extend(batch)
            if len(batch) < 100:
                return rows
        raise GitHubUnavailable('GitHub inventory exceeded the pagination bound')

    def publisher(self, role):
        row = self.settings.view()['roles']['controller' if role == 'recovery' else role]
        slug = row.get('app_slug')
        if not slug:
            raise GitHubUnavailable('Role identity is not verified')
        return slug + '[bot]'

    def target(self, item):
        repository = self.call('controller', item, 'GET', '')
        if repository.get('id') != item['repo_id'] or not isinstance(repository.get('default_branch'), str):
            raise GitHubUnavailable('Repository identity or target branch is unverified')
        return repository['default_branch']

    def issues(self, repo):
        return [i for i in self.pages('controller', repo, '/issues?state=open&labels=' + quote(self.runtime['ready_label']))
                if 'pull_request' not in i]

    def issue(self, item):
        value = self.call('controller', item, 'GET', f"/issues/{item['number']}")
        if value.get('number') != item['number'] or 'pull_request' in value:
            raise GitHubUnavailable('Issue identity could not be verified')
        comments = self.pages('controller', item, f"/issues/{item['number']}/comments")
        requirements = digest({'title': value['title'], 'body': value.get('body') or '',
                               'comments': [(c['id'], c['body']) for c in comments if c.get('user', {}).get('type') == 'User']})
        return value, requirements

    def pulls(self, item):
        return self.pages('controller', item, '/pulls?state=open')

    def pr(self, item, number):
        value = self.call('controller', item, 'GET', f'/pulls/{number}')
        if value.get('number') != number or value.get('base', {}).get('repo', {}).get('id') != item['repo_id']:
            raise GitHubUnavailable('PR repository identity could not be verified')
        return value

    def publication(self, job, requirements, policy):
        """Search the full bounded inventory and accept one exact contract, not links."""
        matches = []
        for pr in self.pulls(job):
            body = pr.get('body') or ''
            try:
                record = submission(body)
            except ValueError:
                continue
            if record['attempt'] == job['id']:
                matches.append(pr)
        if len(matches) != 1:
            return None
        try:
            pr = self.pr(job, matches[0]['number'])
            value = candidate(pr, job, requirements, policy)
            value['branch'] = pr['head']['ref']
            return value
        except (ValueError, TypeError, KeyError):
            return None

    def check(self, item, role, candidate_value, external_id, conclusion, summary):
        name = 'forge/merge-policy' if role == 'controller' else 'forge/' + role
        runs = self.pages(role, item, f"/commits/{candidate_value['head']}/check-runs?filter=all", 'check_runs')
        app_id = self.connection(role)['app_id']
        own = [r for r in runs if r.get('name') == name and r.get('app', {}).get('id') == app_id
               and r.get('external_id') == external_id]
        if len(own) > 1:
            raise GitHubUnavailable('Duplicate check publication needs reconciliation')
        payload = {'name': name, 'external_id': external_id,
                   'status': 'completed' if conclusion else 'in_progress',
                   'output': {'title': name + (': ' + conclusion if conclusion else ': waiting'),
                              'summary': summary[:60000]}}
        if conclusion:
            payload['conclusion'] = conclusion
        if own and own[0].get('head_sha') == candidate_value['head'] and own[0].get('status') == payload['status'] and own[0].get('conclusion') == conclusion and (own[0].get('output') or {}).get('summary') == payload['output']['summary']:
            return own[0]['id']
        if own:
            # Same external ID is the durable publication idempotency key.
            path = '/check-runs/' + str(own[0]['id'])
            self.call(role, item, 'PATCH', path, payload)
        else:
            payload['head_sha'] = candidate_value['head']
            self.call(role, item, 'POST', '/check-runs', payload)
        after = self.pages(role, item, f"/commits/{candidate_value['head']}/check-runs?filter=all", 'check_runs')
        verified = [r for r in after if r.get('name') == name and r.get('app', {}).get('id') == app_id
                    and r.get('external_id') == external_id and r.get('head_sha') == candidate_value['head']
                    and r.get('status') == payload['status'] and (not conclusion or r.get('conclusion') == conclusion)]
        if len(verified) != 1:
            raise GitHubUnavailable('Published check has not been verified')
        return verified[0]['id']

    def verdict(self, job, report):
        candidate_value = job['candidate']
        marker = '<!-- forge-verdict ' + job['id'] + ' -->'
        reviews = self.pages(job['role'], job, f"/pulls/{candidate_value['pr']}/reviews")
        owned = [r for r in reviews if marker in (r.get('body') or '') and r.get('user', {}).get('login') == job['publisher']
                 and r.get('commit_id') == candidate_value['head']]
        if not owned:
            self.call(job['role'], job, 'POST', f"/pulls/{candidate_value['pr']}/reviews",
                      {'commit_id': candidate_value['head'], 'event': 'APPROVE' if report['outcome'] == 'pass' else 'REQUEST_CHANGES',
                       'body': report['summary'] + '\n\n' + marker})
            owned = [r for r in self.pages(job['role'], job, f"/pulls/{candidate_value['pr']}/reviews")
                     if marker in (r.get('body') or '') and r.get('user', {}).get('login') == job['publisher']
                     and r.get('commit_id') == candidate_value['head']]
        expected = 'APPROVED' if report['outcome'] == 'pass' else 'CHANGES_REQUESTED'
        if len(owned) != 1 or owned[0]['state'] != expected:
            raise GitHubUnavailable('Role review publication is unconfirmed or dismissed')
        return self.check(job, job['role'], candidate_value, job['id'],
                          'success' if report['outcome'] == 'pass' else 'failure', report['summary'])

    def human(self, item, pr):
        reviews = self.pages('controller', item, f"/pulls/{pr['number']}/reviews")
        latest = {}
        for review in sorted(reviews, key=lambda r: r['id']):
            if review['state'] in ('APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'):
                latest[review['user']['login'].lower()] = review
        blocked = any(r['state'] == 'CHANGES_REQUESTED' for r in latest.values())
        authorized = set(n.lower() for n in self.runtime['human_reviewers'])
        after = (item.get('candidate') or {}).get('accepted_at', 0)
        def fresh(review):
            if not after:
                return True
            try:
                return datetime.fromisoformat(review['submitted_at'].replace('Z', '+00:00')).timestamp() > int(after)
            except (ValueError, KeyError, TypeError):
                return False
        approved = [r for name, r in latest.items() if name in authorized and r['state'] == 'APPROVED'
                    and r['user'].get('type') == 'User' and r['commit_id'] == pr['head']['sha']
                    and name != pr['user']['login'].lower() and fresh(r)]
        return bool(approved) and not blocked, blocked

    def protections(self, item, candidate_value):
        report = self.protection_report(item, candidate_value)
        return report['available'] and all(row['passed'] for row in report['requirements'])

    def protection_report(self, item, candidate_value):
        """Check the shared review baseline without claiming exclusive repository control."""
        branch = candidate_value['base_ref']
        report = {'branch': branch, 'available': True, 'requirements': []}
        try:
            protection = self.call('controller', item, 'GET', '/branches/' + quote(branch, safe='') + '/protection')
        except GitHubUnavailable as error:
            if error.status not in (403, 404):
                raise
            return {**report, 'available': False, 'requirements': [{
                'id': 'readable', 'passed': False, 'label': 'Read classic branch protection',
                'detail': 'Forge could not read a classic protection rule for this branch. Check that a rule exists and the Controller App has Administration read access. Rulesets-only setups are not supported yet.'}]}
        rows = report['requirements']
        def requirement(key, label, passed, detail):
            rows.append({'id': key, 'label': label, 'passed': bool(passed), 'detail': detail})
        reviews = protection.get('required_pull_request_reviews') or {}
        requirement('reviews', 'Require at least two approving reviews', reviews.get('required_approving_review_count', 0) >= 2,
                    'Enable required pull request reviews and set the approval count to at least two.')
        requirement('stale', 'Dismiss approvals when code changes', reviews.get('dismiss_stale_reviews'),
                    'Enable “Dismiss stale pull request approvals when new commits are pushed”.')
        return report

    def native_human_gate(self, item):
        """Require GitHub to enforce human ownership atomically for escalated merges.

        Custom check success alone cannot retract an approval racing a merge.
        The supported first version uses one wildcard CODEOWNERS rule containing
        only configured humans, plus native required code-owner reviews.
        """
        candidate_value = item['candidate']
        protection = self.call('controller', item, 'GET', '/branches/' + quote(candidate_value['base_ref'], safe='') + '/protection')
        if not (protection.get('required_pull_request_reviews') or {}).get('require_code_owner_reviews'):
            return False
        document = self.call('controller', item, 'GET', '/contents/.github/CODEOWNERS?ref=' + candidate_value['base'])
        try:
            body = base64.b64decode(document['content'], validate=False).decode()
            rules = [line.split('#', 1)[0].split() for line in body.splitlines() if line.split('#', 1)[0].strip()]
        except (ValueError, KeyError, UnicodeError):
            return False
        if len(rules) != 1 or rules[0][0] != '*' or len(rules[0]) < 2:
            return False
        owners = set(rules[0][1:])
        authorized = {'@' + name.lower() for name in self.runtime['human_reviewers']}
        return bool(owners) and {name.lower() for name in owners}.issubset(authorized)

    def gates(self, item):
        candidate_value = item['candidate']
        runs = self.pages('controller', item, f"/commits/{candidate_value['head']}/check-runs?filter=all", 'check_runs')
        for role in ('review', 'audit'):
            accepted = item['evidence'].get(role)
            if not accepted:
                return False
            own = [r for r in runs if r.get('name') == 'forge/' + role
                   and r.get('app', {}).get('id') == self.connection(role)['app_id']]
            if not own:
                return False
            current = max(own, key=lambda r: r['id'])
            if (current['id'] != accepted['check_id'] or current.get('conclusion') != 'success'
                    or current.get('status') != 'completed'):
                return False
        return True

    def merge(self, item):
        candidate_value = item['candidate']
        # The expected SHA makes a concurrent head push a refused merge.
        value = self.call('controller', item, 'PUT', f"/pulls/{candidate_value['pr']}/merge",
                          {'sha': candidate_value['head'], 'merge_method': self.runtime['merge_method']})
        return value.get('merged') is True

    def close_merged_issue(self, item, issue):
        """Only called after independently observing the assigned PR as merged."""
        if issue['state'] == 'open':
            self.call('controller', item, 'PATCH', f"/issues/{item['number']}",
                      {'state': 'closed', 'state_reason': 'completed'})
            current = self.call('controller', item, 'GET', f"/issues/{item['number']}")
            if current.get('state') != 'closed':
                raise GitHubUnavailable('Merged issue closure has not been verified')

    def labels(self, item):
        wanted = 'forge:' + item['state']
        issue = self.call('controller', item, 'GET', f"/issues/{item['number']}")
        labels = {l['name'] for l in issue.get('labels', [])}
        if wanted not in labels:
            known = {l['name'] for l in self.pages('controller', item, '/labels')}
            if wanted not in known:
                self.call('controller', item, 'POST', '/labels', {'name': wanted, 'color': '64748b', 'description': 'Supervisor projection; not authority to advance an issue'})
            self.call('controller', item, 'POST', f"/issues/{item['number']}/labels", {'labels': [wanted]})
            verified = self.call('controller', item, 'GET', f"/issues/{item['number']}")
            if wanted not in {l['name'] for l in verified.get('labels', [])}:
                raise GitHubUnavailable('Pipeline label projection was not verified')
        for label in labels:
            if label.startswith('forge:') and label != wanted:
                self.call('controller', item, 'DELETE', f"/issues/{item['number']}/labels/" + quote(label, safe=''))
