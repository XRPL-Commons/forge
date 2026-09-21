"""Read-only issue inventory and operator-facing decisions, separate from admission."""
from copy import deepcopy
from pathlib import Path
import re
import threading
import time

from ..settings import Settings, read_json, write_json
from ..state import AtomicStore
from .config import load
from .contracts import digest, references_issue
from .github import GitHub, GitHubUnavailable
from .ledger import Ledger

MAX_AGE = 300
LEGACY_SETUP_REASON = 'Configure two approving reviews and stale-approval dismissal before dispatch'


def labels(issue):
    return {r['name'] if isinstance(r, dict) else r for r in issue.get('labels', [])}


def facts(issue):
    return {'number': issue['number'], 'title': issue.get('title', ''),
            'body': (issue.get('body') or '')[:16000],
            'content_hash': digest(issue.get('body') or ''), 'body_truncated': len(issue.get('body') or '') > 16000,
            'state': issue['state'], 'updated_at': issue.get('updated_at'),
            'labels': sorted(labels(issue)),
            'assignees': [a['login'] for a in issue.get('assignees', [])]}


def revision(issue, item):
    public = {k: issue.get(k) for k in ('number', 'title', 'content_hash', 'state', 'assignees')}
    public['labels'] = sorted(l for l in labels(issue) if not l.startswith('forge:'))
    tracked = {k: (item or {}).get(k) for k in ('state', 'active', 'candidate', 'budget_start', 'operator_hold', 'prerequisite_hold', 'environment_hold')}
    return digest({'github': public, 'ledger': tracked})


def dependencies(body):
    # Only explicit prerequisite statements, not arbitrary issue references.
    result = set()
    for line in (body or '').splitlines():
        if re.search(r'\b(?:blocked by\b|depends on\b|dependency:)', line, re.I):
            result.update(int(n) for n in re.findall(r'(?<![\w/])#(\d+)\b', line))
    return sorted(result)


def presentation(issue, item, repo, runtime, prs=(), *, fresh=True):
    item = item or {}
    held = labels(issue).intersection(runtime['hold_labels'])
    linked = [p for p in prs if references_issue(p.get('body'), repo['name'], issue['number'])]
    owned_pr = (item.get('candidate') or {}).get('pr')
    external = [p for p in linked if p['number'] != owned_pr]
    if owned_pr and not any(p['number'] == owned_pr for p in linked):
        linked.append({'number': owned_pr, 'title': 'Forge pull request'})
    state, detail, actions = 'backlog', 'Not queued for Forge.', []
    setup = bool(item.get('prerequisite_hold')) or item.get('reason') == LEGACY_SETUP_REASON
    cleanup = bool(item.get('cleanup_pending'))
    if issue['state'] == 'closed' or item.get('state') == 'merged':
        state, detail = 'completed', 'Completed and merged.' if item.get('state') == 'merged' else 'Closed on GitHub.'
    elif item.get('operator_request'):
        state, detail = 'waiting', 'Applying your decision. Forge will confirm it before continuing.'
    elif item.get('active'):
        state, detail = 'working', {'build': 'Building', 'review': 'Reviewing', 'audit': 'Security review', 'merge': 'Merging', 'recovery': 'Investigating a failed attempt'}.get(item.get('active_role'), 'Working')
    elif cleanup:
        state, detail = 'waiting', 'Finishing cleanup from the previous attempt.'
    elif held:
        state, detail = 'attention', 'Paused by ' + ', '.join(sorted(held)) + ' on GitHub.'
        if held == {'hold'}:
            actions = ['resume']
        else:
            detail += ' Resolve the dependency before releasing this hold.'
    elif item.get('environment_hold'):
        state, detail, actions = 'attention', 'Forge’s build environment needs attention. The pull request and code-retry budget are preserved. Repair the environment, then recheck.', ['recheck']
    elif setup:
        state = 'attention'
        detail = ('Repository setup now passes. Recheck to resume.' if repo.get('protection_ready') and repo.get('access_ready')
                  else 'Repository setup needs attention.')
        actions = ['recheck'] if repo.get('protection_ready') and repo.get('access_ready') else []
    elif external or (not owned_pr and (issue.get('assignees') or 'wip' in labels(issue))):
        state, detail = 'attention', 'Existing work needs review before Forge starts another attempt.'
    elif item.get('state') == 'cancelled':
        state, detail, actions = 'attention', 'The previous attempt was stopped. Review before resuming.', ['resume']
    elif item.get('state') == 'failed':
        state, detail, actions = 'attention', item.get('reason') or 'The last attempt failed.', ['retry']
    elif item.get('operator_hold'):
        backlog = item['operator_hold']['kind'] == 'backlog'
        state, detail, actions = ('backlog', 'Not queued for Forge.', ['queue']) if backlog else ('attention', 'Paused by an operator.', ['resume'])
    elif item.get('state') == 'hold':
        state, detail = 'attention', item.get('reason') or 'An operator decision is needed.'
    elif item.get('state') == 'approval_needed':
        state, detail = 'attention', 'The pull request needs a human review.'
    elif item.get('state') in ('built', 'reviewed', 'audited', 'investigating'):
        state, detail = 'queued', {'built': 'Waiting for review.', 'reviewed': 'Waiting for security review.', 'audited': 'Checking merge requirements.', 'investigating': 'Waiting for a failure investigation.'}[item['state']]
        actions = ['pause'] if item['state'] != 'investigating' else []
    elif runtime['ready_label'] in labels(issue):
        state, detail, actions = 'queued', 'Queued for Forge.', ['backlog', 'pause']
    else:
        actions = ['queue']
    if state == 'queued' and item.get('retry_after', 0) > time.time():
        detail = ('Waiting for a coding account. Forge will retry automatically.'
                  if item.get('reason') in ('blocked_auth', 'blocked_quota', 'Role credentials unavailable')
                  else 'Waiting before another check. Forge will retry automatically.')
    if state == 'queued' and not runtime['enabled']:
        detail = 'Forge is paused. This issue stays queued.'
    if not fresh:
        actions = []
    if not repo.get('enabled') or not repo.get('access_ready'):
        actions = []
        if state not in ('completed', 'working'):
            state, detail = 'attention', 'Repository access or activation needs attention.'
    return {'status': state, 'explanation': detail, 'actions': actions, 'setup_needed': setup or not repo.get('access_ready'),
            'environment_blocker': (item.get('environment_hold') or {}).get('detail'),
            'pull_requests': [{'number': p['number'], 'title': p.get('title', '')} for p in linked],
            'dependencies': dependencies(issue.get('body')), 'fresh': fresh}


class IssueInventory:
    def __init__(self, root, *, settings=None, github=None, clock=time.time):
        self.root = Path(root)
        self.settings = settings or Settings(root)
        self.clock = clock
        self.runtime = load(root)
        self.github = github or GitHub(self.settings, self.runtime)
        self.path = self.root / 'state/issues/inventory.json'

    def read(self):
        try:
            return read_json(self.path)
        except (OSError, ValueError):
            return {'repositories': {}}

    def refresh(self, force=False):
        try:
            with self.settings.locked('issue-inventory', blocking=False):
                if self.settings.paused():
                    return
                data = self.read()
                self.github.deadline = time.monotonic() + 60
                for repo in self.settings.view()['repositories']:
                    if not repo.get('enabled') or repo['access'].get('controller') != 'ready':
                        continue
                    key = str(repo['id'])
                    old = data.setdefault('repositories', {}).get(key, {})
                    identity = digest({'name': repo['name'], 'connections': self.settings.load()['connections']})
                    if old.get('identity') != identity:
                        old = {}
                    if not force and old.get('complete') and 0 <= self.clock() - old.get('at', 0) < 60:
                        continue
                    entry = deepcopy(old)
                    entry.update(identity=identity, name=repo['name'], refreshing=True, error=None)
                    scan = entry.setdefault('scan', {'state': 'open', 'page': 1, 'issues': {}})
                    item = {'repo': repo['name'], 'repo_id': repo['id']}
                    try:
                        while time.monotonic() < self.github.deadline:
                            if scan['page'] > 100:
                                raise GitHubUnavailable('Issue inventory exceeds the 10,000-item limit for this view.')
                            batch = self.github.call('controller', item, 'GET', f"/issues?state={scan['state']}&sort=created&direction=asc&per_page=100&page={scan['page']}")
                            if not isinstance(batch, list):
                                raise GitHubUnavailable('Issue inventory is unavailable.')
                            for issue in batch:
                                if 'pull_request' not in issue:
                                    row = facts(issue)
                                    scan['issues'][str(row['number'])] = row
                            entry['issues'] = {**old.get('issues', {}), **scan['issues']}
                            entry.update(at=self.clock(), complete=False)
                            if len(batch) < 100:
                                if scan['state'] == 'open':
                                    scan.update(state='closed', page=1)
                                else:
                                    entry['pulls'] = [{'number': p['number'], 'title': p.get('title', ''), 'body': (p.get('body') or '')[:262144]}
                                                      for p in self.github.pulls(item)]
                                    entry.update(issues=scan['issues'], complete=True, refreshing=False)
                                    entry.pop('scan', None)
                                    break
                            else:
                                scan['page'] += 1
                    except (GitHubUnavailable, ValueError, KeyError, OSError):
                        entry.update(error='Could not finish refreshing. Previously received issues are still shown.', refreshing=False)
                    data['repositories'][key] = entry
                    write_json(self.path, data)
        except BlockingIOError:
            pass

    def start_refresh(self, force=False):
        threading.Thread(target=self.refresh, args=(force,), daemon=True).start()

    def view(self, *, repo_id=None, state='open', query='', page=1, key=None):
        if state not in ('open', 'completed', 'all', 'backlog', 'queued', 'working', 'attention', 'waiting') or type(page) is not int or page < 1:
            raise ValueError('Choose a valid issue view')
        data = self.read()
        ledger = Ledger(self.root / 'state/factory').read()
        repositories = [r for r in self.settings.view()['repositories'] if r['enabled']]
        rows, scopes = [], []
        for repo in repositories:
            entry = data.get('repositories', {}).get(str(repo['id']), {})
            identity = digest({'name': repo['name'], 'connections': self.settings.load()['connections']})
            if entry.get('identity') != identity:
                entry = {}
            fresh = bool(entry.get('at') and 0 <= self.clock() - entry['at'] < MAX_AGE and not entry.get('error'))
            scopes.append({'id': repo['id'], 'name': repo['name'], 'complete': bool(entry.get('complete')), 'fresh': fresh,
                           'at': entry.get('at'), 'error': entry.get('error')})
            if repo_id is not None and repo['id'] != repo_id:
                continue
            combined = dict(entry.get('issues', {}))
            for item in ledger['issues'].values():
                if item['repo_id'] == repo['id'] and str(item['number']) not in combined:
                    combined[str(item['number'])] = {'number': item['number'], 'title': item.get('title', ''), 'body': '',
                                                    'state': 'closed' if item['state'] == 'merged' else 'open', 'labels': [], 'assignees': []}
            for issue in combined.values():
                issue_key = f"{repo['id']}:{issue['number']}"
                item = deepcopy(ledger['issues'].get(issue_key))
                if item:
                    recovery = next((j for j in ledger['jobs'].values() if j['issue_key'] == issue_key
                                     and j['role'] == 'recovery' and j.get('phase') == 'running'
                                     and not j['cleanup'] and not j.get('outcome')), None)
                    if recovery and not item['active']:
                        item['active'] = recovery['id']
                    item['active_role'] = ledger['jobs'].get(item.get('active'), {}).get('role')
                    item['cleanup_pending'] = any(j['issue_key'] == issue_key and not j['cleanup'] for j in ledger['jobs'].values())
                observed = (item or {}).get('github_observation')
                if observed and observed['at'] > entry.get('at', 0):
                    issue = observed['issue']
                ready = fresh and str(issue['number']) in entry.get('scan', {}).get('issues', entry.get('issues', {}))
                row = {**issue, 'key': issue_key, 'repo': repo['name'], 'repo_id': repo['id'],
                       **presentation(issue, item, repo, self.runtime, entry.get('pulls', []), fresh=ready),
                       'revision': revision(issue, item), 'setup_url': repo['setup_url'],
                       'history': [{k: e[k] for k in ('at', 'kind', 'reason') if k in e} for e in ledger['events'] if e['issue'] == issue_key][-15:],
                       'active': item.get('active') if item else None,
                       'reviews': [{'role': role, 'outcome': value.get('outcome'), 'summary': value.get('summary', '')[:1000]}
                                   for role, value in (item or {}).get('evidence', {}).items() if role in ('review', 'audit')]}
                if key is not None:
                    if key == issue_key:
                        return {'issue': row, 'repositories': scopes, 'actions': self.action_statuses()}
                    continue
                if state == 'open' and row['status'] == 'completed' or state == 'completed' and row['status'] != 'completed':
                    continue
                if state not in ('open', 'completed', 'all') and row['status'] != state:
                    continue
                if query.lower() not in f"{row['repo']} #{row['number']} {row['title']}".lower():
                    continue
                rows.append(row)
        if key is not None:
            raise ValueError('This issue is not in a managed repository. Refresh the issue list.')
        order = {'attention': 0, 'working': 1, 'waiting': 2, 'queued': 3, 'backlog': 4, 'completed': 5}
        rows.sort(key=lambda r: (order[r['status']], r['repo'].lower(), r['number']))
        return {'issues': [{k: v for k, v in r.items() if k not in ('body', 'history', 'reviews')} for r in rows[(page-1)*50:page*50]],
                'total': len(rows), 'page': page, 'pages': max(1, (len(rows)+49)//50), 'repositories': scopes,
                'complete': all(r['complete'] and r['fresh'] for r in scopes if repo_id is None or r['id'] == repo_id), 'actions': self.action_statuses()}

    def action_statuses(self):
        with AtomicStore(self.root / 'state/issues', 'actions').transaction() as data:
            ordered = sorted(data.items(), key=lambda pair: pair[1]['created_at'])
            recent = {key for key, value in ordered[-20:]}
            return [{'id': key, 'key': value['key'], 'status': value['status'], 'message': value.get('message')}
                    for key, value in ordered if value['status'] == 'pending' or key in recent]
