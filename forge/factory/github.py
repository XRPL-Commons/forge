"""Role-scoped GitHub effects and authoritative evidence, never terminal inference."""
import json
import base64
from datetime import datetime
import time
from urllib.parse import quote

from ..app_access import AppClient, AccessError, OPTIONAL_PERMISSIONS, ROLE_PERMISSIONS
from .contracts import SHA, candidate, digest, submission

# Ancestry between two recorded commits never changes; one process asks once.
COMPARISONS = {}


class GitHubUnavailable(RuntimeError):
    def __init__(self, message, *, status=None):
        super().__init__(message)
        self.status = status


class GitHubBudgetExhausted(GitHubUnavailable):
    """The cycle's observation budget is spent; the next cycle resumes. Not an error to report."""


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

    def token(self, role, repo_id, *, worker=False, extra=None):
        connection = self.connection(role, repo_id)
        key = (role, repo_id, worker, digest(connection), digest(extra or {}))
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
        # Optional grants widen one call only; a role's baseline stays mintable.
        permissions.update(extra or {})
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

    def call(self, role, repo, method, suffix, payload=None, *, extra=None):
        if time.monotonic() >= getattr(self, 'deadline', float('inf')):
            raise GitHubBudgetExhausted('Cycle observation budget reached; resume next cycle')
        try:
            return self.client.request(method, f"/repos/{repo['repo']}" + suffix,
                                       self.token(role, repo['repo_id'], extra=extra), payload)
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

    def workflows_digest(self, item):
        """Identity of the deploy surface: the workflow files at the default branch head."""
        repository = self.call('controller', item, 'GET', '')
        branch = repository.get('default_branch')
        if repository.get('id') != item['repo_id'] or not isinstance(branch, str) or not branch:
            raise GitHubUnavailable('Repository identity or target branch is unverified')
        head = (self.call('controller', item, 'GET', '/branches/' + quote(branch, safe='')).get('commit') or {}).get('sha')
        if not isinstance(head, str) or not SHA.fullmatch(head):
            raise GitHubUnavailable('Default branch head could not be verified')
        try:
            files = self.call('controller', item, 'GET', '/contents/.github/workflows?ref=' + quote(head, safe=''))
        except GitHubUnavailable as error:
            if error.status != 404:
                raise
            files = []  # A repository without workflows has a stable, empty digest.
        if not isinstance(files, list) or any(not isinstance(f, dict) or not isinstance(f.get('path'), str)
                                              or not isinstance(f.get('sha'), str) for f in files):
            raise GitHubUnavailable('Workflow inventory is malformed')
        return digest(sorted([f['path'], f['sha']] for f in files)), head

    def file_issue(self, item, title, body, labels):
        """One operator-confirmed proposal, never labelled ready: a human starts it."""
        value = self.call('controller', item, 'POST', '/issues',
                          {'title': title, 'body': body, 'labels': list(labels)})
        number = (value or {}).get('number')
        if type(number) is not int or number <= 0:
            raise GitHubUnavailable('The filed deployment issue could not be verified')
        return number

    def deployments(self, item, environment, limit=5):
        """Newest first, on the optional grant the caller has already verified."""
        suffix = f'/deployments?per_page={int(limit)}'
        if environment:
            suffix += '&environment=' + quote(str(environment), safe='')
        rows = self.call('controller', item, 'GET', suffix, extra=OPTIONAL_PERMISSIONS['controller'])
        if not isinstance(rows, list):
            raise GitHubUnavailable('GitHub deployment inventory is malformed')
        for row in rows:
            if (not isinstance(row, dict) or type(row.get('id')) is not int
                    or not isinstance(row.get('sha'), str) or not SHA.fullmatch(row['sha'])):
                raise GitHubUnavailable('GitHub deployment inventory is malformed')
        return sorted(rows, key=lambda row: row['id'], reverse=True)

    def deployment_statuses(self, item, deployment_id):
        """Newest first by identifier: the latest status is never an ordering guess."""
        rows = self.call('controller', item, 'GET', f'/deployments/{int(deployment_id)}/statuses?per_page=5',
                         extra=OPTIONAL_PERMISSIONS['controller'])
        if not isinstance(rows, list):
            raise GitHubUnavailable('GitHub deployment statuses are malformed')
        for row in rows:
            if (not isinstance(row, dict) or type(row.get('id')) is not int
                    or not isinstance(row.get('state'), str)):
                raise GitHubUnavailable('GitHub deployment statuses are malformed')
        return sorted(rows, key=lambda row: row['id'], reverse=True)

    def contains(self, item, ancestor, descendant):
        """Is the merge in the history of the observed commit? Asked once per pair."""
        key = (item['repo_id'], ancestor, descendant)
        if key in COMPARISONS:
            return COMPARISONS[key]
        value = self.call('controller', item, 'GET',
                          f"/compare/{quote(ancestor, safe='')}...{quote(descendant, safe='')}")
        status = (value or {}).get('status')
        if status not in ('ahead', 'behind', 'identical', 'diverged'):
            raise GitHubUnavailable('GitHub commit comparison is malformed')
        if len(COMPARISONS) > 4096:
            COMPARISONS.clear()
        COMPARISONS[key] = status in ('ahead', 'identical')
        return COMPARISONS[key]

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

    def collaborators(self, item):
        """People who can approve here: push access, offered as reviewer choices."""
        rows = self.pages('controller', item, '/collaborators?permission=push')
        return [{'login': row['login'], 'name': str(row.get('name') or '')[:120]}
                for row in rows if isinstance(row, dict) and isinstance(row.get('login'), str)][:500]

    def recent_authors(self, item):
        """People whose pull requests were recently merged here: they demonstrably
        ship to this repository, including org members GitHub does not list as
        collaborators to an App."""
        rows = self.call('controller', item, 'GET', '/pulls?state=closed&sort=updated&direction=desc&per_page=100')
        logins = []
        for row in rows if isinstance(rows, list) else []:
            user = row.get('user') or {} if isinstance(row, dict) else {}
            login = user.get('login')
            if row.get('merged_at') and isinstance(login, str) and user.get('type') != 'Bot' and not login.endswith('[bot]') and login not in logins:
                logins.append(login)
        return logins[:30]

    def label_events(self, item, number):
        """Who last applied each label, newest first."""
        rows = self.call('controller', item, 'GET', f'/issues/{number}/events?per_page=100')
        seen, out = set(), []
        for row in reversed(rows if isinstance(rows, list) else []):
            label = ((row or {}).get('label') or {}).get('name')
            if row.get('event') == 'labeled' and isinstance(label, str) and label not in seen:
                seen.add(label)
                out.append({'label': label, 'login': (row.get('actor') or {}).get('login'), 'at': row.get('created_at')})
        return out

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

    def ci_evidence(self, job):
        """Bounded check evidence fetched by the controller, not a worker token.

        A snapshot is context, never an approval or a replacement for GitHub's
        required-check gates. No Actions permission or log credential is needed.
        """
        selected = job.get('candidate')
        if not selected:
            return None
        snapshots = {}
        for revision in dict.fromkeys((selected['head'], selected['base'])):
            rows = self.pages('controller', job, f'/commits/{revision}/check-runs?filter=latest', 'check_runs')
            if len(rows) > 100:
                raise GitHubUnavailable('CI snapshot exceeded its check limit')
            checks = []
            for row in rows:
                if row.get('head_sha') != revision:
                    raise GitHubUnavailable('CI check belongs to a different commit')
                if row.get('name', '').startswith('forge/'):
                    continue  # Do not treat our own pending verdict as project CI.
                output = row.get('output') or {}
                check = {key: row.get(key) for key in ('id', 'name', 'status', 'conclusion', 'details_url')}
                check['app_id'] = (row.get('app') or {}).get('id')
                check['summary'] = str(output.get('summary') or '')[:2000]
                check['annotations'] = []
                if row.get('conclusion') in ('failure', 'timed_out', 'action_required'):
                    annotations = self.call('controller', job, 'GET', f"/check-runs/{int(row['id'])}/annotations?per_page=20")
                    if not isinstance(annotations, list):
                        raise GitHubUnavailable('CI annotations are malformed')
                    check['annotations'] = [{key: str(a.get(key) or '')[:2000] for key in
                                             ('path', 'start_line', 'annotation_level', 'title', 'message')}
                                            for a in annotations[:20]]
                    check['annotations_truncated'] = int(output.get('annotations_count') or 0) > 20
                checks.append(check)
            snapshots[revision] = checks
        value = {'observed_at': self.clock(), 'head': selected['head'], 'base': selected['base'],
                 'checks': snapshots, 'scope': 'GitHub check runs and bounded annotations; not Actions logs or legacy commit statuses'}
        if len(json.dumps(value)) > 128000:
            raise GitHubUnavailable('CI evidence exceeded its size limit')
        return value

    def check(self, item, role, candidate_value, external_id, conclusion, summary):
        name = 'forge/merge-policy' if role == 'controller' else 'forge/' + role
        runs = self.pages(role, item, f"/commits/{candidate_value['head']}/check-runs?filter=all", 'check_runs')
        app_id = self.connection(role)['app_id']
        own = [r for r in runs if r.get('name') == name and r.get('app', {}).get('id') == app_id
               and r.get('external_id') == external_id]
        if len(own) > 1:
            raise GitHubUnavailable('Duplicate check publication needs reconciliation')
        # GitHub retains a completed check's conclusion when asked to mark it
        # in_progress again. Keep that verdict until a new revision/verdict is
        # available; trying to reopen it makes verification fail and blocks the
        # very build that must repair it. Review/audit attempts have new IDs.
        if own and own[0].get('head_sha') == candidate_value['head'] and own[0].get('status') == 'completed' and conclusion is None:
            return own[0]['id']
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
        """Read effective GitHub rules; never confuse the baseline with a bypass audit."""
        branch = candidate_value['base_ref']
        repo = item.get('repo', '')
        report = {'repo_id': item.get('repo_id'), 'branch': branch, 'observed_at': self.clock(),
                  'available': True, 'requirements': [], 'sources': [], 'errors': [],
                  'additional_rules': [], 'code_owner_reviews': False,
                  'bypass': {'status': 'operator_review', 'detail':
                      'GitHub may hide bypass permissions. Review who can bypass these rules on GitHub.'}}
        reviews = []
        try:
            protection = self.call('controller', item, 'GET', '/branches/' + quote(branch, safe='') + '/protection')
            if not isinstance(protection, dict):
                raise GitHubUnavailable('Malformed branch protection')
            review = protection.get('required_pull_request_reviews')
            if review is not None and not isinstance(review, dict):
                raise GitHubUnavailable('Malformed review protection')
            source = {'kind': 'classic', 'name': 'Branch protection',
                      'url': f'https://github.com/{repo}/settings/branches'}
            report['sources'].append(source)
            if review:
                reviews.append((review, source))
        except GitHubBudgetExhausted:
            raise
        except GitHubUnavailable as error:
            if error.status != 404:
                report['errors'].append({'source': 'classic', 'detail':
                    'Branch protection could not be read. Check Controller access and try again.'})
        try:
            rules = self.pages('controller', item, '/rules/branches/' + quote(branch, safe=''))
            for rule in rules:
                if not isinstance(rule, dict) or not isinstance(rule.get('type'), str):
                    raise GitHubUnavailable('Malformed ruleset observation')
                identifier = rule.get('ruleset_id')
                source = {'kind': 'ruleset', 'name': rule.get('ruleset_source') or 'Repository rules',
                          'id': identifier, 'source_type': rule.get('ruleset_source_type'),
                          'url': f'https://github.com/{repo}/rules/{identifier}' if type(identifier) is int
                                 else f'https://github.com/{repo}/rules'}
                if source not in report['sources']:
                    report['sources'].append(source)
                if rule['type'] == 'pull_request':
                    parameters = rule.get('parameters')
                    if not isinstance(parameters, dict):
                        raise GitHubUnavailable('Malformed pull request rule')
                    reviews.append(({**parameters, 'require_code_owner_reviews':
                                     parameters.get('require_code_owner_review', False)}, source))
                else:
                    report['additional_rules'].append({'type': rule['type'], 'source': source})
        except GitHubBudgetExhausted:
            raise
        except GitHubUnavailable:
            report['errors'].append({'source': 'rulesets', 'detail':
                'Repository rules could not be checked. Check GitHub access and try again.'})
        valid = all(type(r.get('required_approving_review_count', 0)) is int
                    and 0 <= r.get('required_approving_review_count', 0) <= 100
                    and type(r.get('dismiss_stale_reviews', r.get('dismiss_stale_reviews_on_push', False))) is bool
                    and type(r.get('require_code_owner_reviews', False)) is bool for r, _ in reviews)
        if not valid:
            report['errors'].append({'source': 'reviews', 'detail': 'GitHub returned incomplete review settings. Check again.'})
        report['available'] = not report['errors']
        if not report['available']:
            report['requirements'] = [{'id': 'readable', 'passed': False,
                'label': 'Check repository review rules', 'detail': report['errors'][0]['detail'], 'sources': []}]
            return report
        count = max((r.get('required_approving_review_count', 0) for r, _ in reviews), default=0)
        stale = [(r, s) for r, s in reviews if r.get('dismiss_stale_reviews', r.get('dismiss_stale_reviews_on_push', False))]
        report['code_owner_reviews'] = any(r.get('require_code_owner_reviews', False) for r, _ in reviews)
        report['required_approvals'] = count
        report['requirements'] = [
            {'id': 'reviews', 'label': 'At least two approving reviews', 'passed': count >= 2,
             'detail': f'{count} approvals required.' if count >= 2 else 'Require pull requests with at least two approving reviews.',
             'sources': [s for r, s in reviews if r.get('required_approving_review_count', 0) >= 2]},
            {'id': 'stale', 'label': 'Fresh approvals when code changes', 'passed': bool(stale),
             'detail': 'Approvals are dismissed when new commits are pushed.' if stale else
                       'Dismiss stale pull request approvals when new commits are pushed.',
             'sources': [s for _, s in stale]}]
        return report

    def native_human_gate(self, item):
        """Require GitHub to enforce human ownership atomically for escalated merges.

        Custom check success alone cannot retract an approval racing a merge.
        The supported first version uses one wildcard CODEOWNERS rule containing
        only configured humans, plus native required code-owner reviews.
        """
        candidate_value = item['candidate']
        report = self.protection_report(item, candidate_value)
        if not report['available'] or not report['code_owner_reviews']:
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

    MERGE_REFUSALS = {
        405: 'branch rules still block it (approvals or checks required, and the Forge controller is not a bypass actor)',
        409: 'the head moved since verification',
        404: 'the controller cannot see the pull request',
    }

    def merge(self, item):
        candidate_value = item['candidate']
        # The expected SHA makes a concurrent head push a refused merge.
        try:
            value = self.call('controller', item, 'PUT', f"/pulls/{candidate_value['pr']}/merge",
                              {'sha': candidate_value['head'], 'merge_method': self.runtime['merge_method']})
        except GitHubUnavailable as error:
            if error.status is None:
                raise
            # A refused merge is the one GitHub error an operator can act on, so
            # it names the pull request and the likely rule instead of the
            # generic observation message.
            why = self.MERGE_REFUSALS.get(error.status, f'GitHub returned HTTP {error.status}')
            raise GitHubUnavailable(f"GitHub refused to merge {item['repo']}#{candidate_value['pr']}: {why}",
                                    status=error.status) from None
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
