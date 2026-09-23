"""Read-only issue inventory and operator-facing decisions, separate from admission."""
from copy import deepcopy
from pathlib import Path
import re
import threading
import time

from ..settings import Settings, read_json, write_json
from ..state import AtomicStore
from .config import load
from .contracts import HEADLINE_LIMIT, clamp, digest, plain_text, references_issue
from .github import GitHub, GitHubUnavailable
from .ledger import Ledger
from .ledger import APPROACH_HOLD

MAX_AGE = 300
COLLABORATOR_AGE = 600
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


# Plain language for the issue page: one headline sentence and at most three
# short sentences, for a reader who does not read code. Forge writes them for
# every state a worker has not spoken for; {why} carries the reason the issue
# is where it is, so nobody has to open Details to learn it.
SUMMARIES = {
    'merged': ('The work is merged',
               'The pull request was merged and the issue is closed. Nothing more is needed here.'),
    'closed': ('The issue is closed on GitHub',
               'This issue was closed outside Forge. Forge is not working on it.'),
    'applying': ('Applying your decision',
                 'Forge has your decision and is confirming it on GitHub. It carries on by itself once that is done.'),
    'build': ('Writing the change',
              'A worker is writing the code for this issue. Forge opens a pull request and asks for a review when it is ready. Nothing is needed from you.'),
    'review': ('The change is being reviewed',
               'A reviewer is reading the pull request. The security check comes next if it passes. Nothing is needed from you.'),
    'audit': ('The change is being checked for security',
              'A security reviewer is going over the pull request. Forge merges it afterwards unless a person has to decide. Nothing is needed from you.'),
    'merge': ('Merging the pull request',
              'Every check passed and Forge is merging the pull request. The issue closes once GitHub confirms the merge.'),
    'recovery': ('Looking into the failed attempt',
                 'Forge is working out why the last attempt failed. It then tries a different approach or asks you to decide. Nothing is needed from you yet.'),
    'working': ('Forge is working on this issue',
                'A worker is busy on this issue. This page says what happened as soon as the step finishes.'),
    'cleanup': ('Finishing the previous attempt',
                'Forge is clearing up after the last attempt. The next step starts as soon as that is done.'),
    'label_hold': ('Paused by a label on GitHub',
                   '{why} Forge leaves the issue alone until that label is removed.'),
    'environment_hold': ('Stopped: Forge’s working environment needs attention',
                         'The worker could not run properly, so Forge stopped before finishing. The pull request and the remaining attempts are kept. Repair the environment, then recheck the issue.'),
    'setup_ready': ('Ready to start once you recheck',
                    'The repository settings Forge needs are in place again. Recheck the issue and Forge carries on from where it stopped.'),
    'setup': ('Waiting for the repository settings Forge needs',
              'Forge cannot work here until the repository’s review settings are in place. Open repository setup to put them right. Forge resumes after a recheck.'),
    'external': ('Someone else is already working on this',
                 'This issue already has work or an assignee of its own. Forge stays away so nothing is done twice. Review that work before letting Forge start.'),
    'cancelled': ('The previous attempt was stopped',
                  'Someone stopped the last attempt and nothing is running now. Review where the issue stands, then release it when Forge should try again.'),
    'failed': ('Stopped: the last attempt failed',
               'Forge could not finish this issue. {why} It will not try again until you retry it.'),
    'operator_hold': ('Paused by an operator',
                      'Someone paused this issue. {why} Forge starts again when you release the hold.'),
    'hold': ('Forge has stopped work on this issue',
             '{why} Forge does nothing more here until someone releases the hold.'),
    'approach_hold': ('Stopped: the same approach keeps failing',
                      'Forge tried the same idea more than once and it did not work. Describe a different approach on the issue, then retry it.'),
    'approval_needed': ('Waiting for a person to approve the pull request',
                        'The change is finished and needs a human approval before it can be merged. {why} Approve it on GitHub, or ask someone to review it.'),
    'built': ('Built and waiting for review',
              'The change is written and its pull request is open. Forge starts the review when a reviewer is free.'),
    'reviewed': ('Reviewed and waiting for the security check',
                 'The review passed. Forge starts the security check when a worker is free.'),
    'audited': ('Checking that it can be merged',
                'Both reviews passed. Forge is checking the repository’s merge requirements before it merges.'),
    'investigating': ('Waiting for the failed attempt to be looked into',
                      'The last attempt failed and Forge will work out why. It decides afterwards whether to try again. Nothing is needed from you yet.'),
    'queued': ('Waiting for a builder',
               'The issue is queued. Forge starts it when a worker and memory are free.'),
    'backlog': ('Not queued for Forge',
                'This issue is not in Forge’s queue. Queue it when you want Forge to work on it.'),
    'waiting_account': ('Waiting for a coding account',
                        'Every coding account is busy or signed out. Forge tries again by itself as soon as one is free.'),
    'waiting_check': ('Waiting before the next check',
                      'Forge paused briefly before looking at this issue again. It carries on by itself.'),
    'paused': ('Forge is paused',
               'This issue stays queued while Forge is paused. It starts when Forge is switched back on.'),
    'repo_blocked': ('Waiting for repository access',
                     'Forge cannot reach this repository, or it is not enabled for Forge. Put its access or activation right. Forge resumes afterwards.'),
}
# The worker's own words lead where the worker is the one who knows.
WORKER_LED = ('failed', 'environment_hold')


def sentence(text):
    text = ' '.join(str(text or '').split())
    return text if not text or text[-1] in '.!?' else text + '.'


def forge_summary(case, why=''):
    headline, plain = SUMMARIES[case]
    # The reason comes second, so the clamp never drops it for Forge's own words.
    return {'headline': clamp(headline, HEADLINE_LIMIT),
            'plain': plain_text(plain.format(why=sentence(plain_text(why, 200, 2))).replace('  ', ' ').strip())}


def why_for(case, item):
    """The reason this issue is where it is, in the words Forge already has."""
    if case == 'approval_needed':
        return item.get('reason') or (item.get('evidence') or {}).get('audit', {}).get('human_reason') or ''
    if case in ('failed', 'hold', 'operator_hold'):
        return item.get('reason') or ''
    return ''


def summary_for(case, item, held):
    written = ((item.get('environment_hold') or {}).get('summary') if case == 'environment_hold'
               else item.get('last_summary') if case in WORKER_LED else None)
    if written and written.get('headline'):
        return {'headline': clamp(written['headline'], HEADLINE_LIMIT), 'plain': plain_text(written.get('plain'))}
    why = ('The GitHub issue is labelled ' + ', '.join(sorted(held)) if case == 'label_hold'
           else why_for(case, item))
    return forge_summary(case, why)


def presentation(issue, item, repo, runtime, prs=(), *, fresh=True):
    item = item or {}
    held = labels(issue).intersection(runtime['hold_labels'])
    linked = [p for p in prs if references_issue(p.get('body'), repo['name'], issue['number'])]
    owned_pr = (item.get('candidate') or {}).get('pr')
    external = [p for p in linked if p['number'] != owned_pr]
    if owned_pr and not any(p['number'] == owned_pr for p in linked):
        linked.append({'number': owned_pr, 'title': 'Forge pull request'})
    state, detail, actions, case = 'backlog', 'Not queued for Forge.', [], 'backlog'
    setup = bool(item.get('prerequisite_hold')) or item.get('reason') == LEGACY_SETUP_REASON
    cleanup = bool(item.get('cleanup_pending'))
    if issue['state'] == 'closed' or item.get('state') == 'merged':
        merged = item.get('state') == 'merged'
        state, detail, case = 'completed', 'Completed and merged.' if merged else 'Closed on GitHub.', 'merged' if merged else 'closed'
    elif item.get('operator_request'):
        state, detail, case = 'waiting', 'Applying your decision. Forge will confirm it before continuing.', 'applying'
    elif item.get('active'):
        state, detail = 'working', {'build': 'Building', 'review': 'Reviewing', 'audit': 'Security review', 'merge': 'Merging', 'recovery': 'Investigating a failed attempt'}.get(item.get('active_role'), 'Working')
        case = item.get('active_role') if item.get('active_role') in SUMMARIES else 'working'
    elif cleanup:
        state, detail, case = 'waiting', 'Finishing cleanup from the previous attempt.', 'cleanup'
    elif held:
        state, detail, case = 'attention', 'Paused by ' + ', '.join(sorted(held)) + ' on GitHub.', 'label_hold'
        if held == {'hold'}:
            actions = ['resume']
        else:
            detail += ' Resolve the dependency before releasing this hold.'
    elif item.get('environment_hold'):
        state, detail, actions = 'attention', 'Forge’s build environment needs attention. The pull request and code-retry budget are preserved. Repair the environment, then recheck.', ['recheck']
        case = 'environment_hold'
    elif setup:
        state = 'attention'
        ready = repo.get('protection_ready') and repo.get('access_ready')
        detail = 'Repository setup now passes. Recheck to resume.' if ready else 'Repository setup needs attention.'
        actions, case = (['recheck'] if ready else []), ('setup_ready' if ready else 'setup')
    elif external or (not owned_pr and (issue.get('assignees') or 'wip' in labels(issue))):
        state, detail, case = 'attention', 'Existing work needs review before Forge starts another attempt.', 'external'
    elif item.get('state') == 'cancelled':
        state, detail, actions, case = 'attention', 'The previous attempt was stopped. Review before resuming.', ['resume'], 'cancelled'
    elif item.get('state') == 'failed':
        state, detail, actions, case = 'attention', item.get('reason') or 'The last attempt failed.', ['retry'], 'failed'
    elif item.get('operator_hold'):
        backlog = item['operator_hold']['kind'] == 'backlog'
        state, detail, actions = ('backlog', 'Not queued for Forge.', ['queue']) if backlog else ('attention', 'Paused by an operator.', ['resume'])
        case = 'backlog' if backlog else 'operator_hold'
    elif item.get('state') == 'hold':
        state, detail, case = 'attention', item.get('reason') or 'An operator decision is needed.', 'hold'
        if item.get('reason') == APPROACH_HOLD:
            detail += ' Describe the new approach on the issue, then retry with that decision.'
            actions, case = ['retry'], 'approach_hold'
    elif item.get('state') == 'approval_needed':
        state, detail, case = 'attention', 'The pull request needs a human review.', 'approval_needed'
        # Asking a named human for that review is the only decision on offer;
        # the approval itself still has to reach Forge through GitHub.
        actions = ['assign_reviewer'] if owned_pr else []
    elif item.get('state') in ('built', 'reviewed', 'audited', 'investigating'):
        state, detail = 'queued', {'built': 'Waiting for review.', 'reviewed': 'Waiting for security review.', 'audited': 'Checking merge requirements.', 'investigating': 'Waiting for a failure investigation.'}[item['state']]
        actions, case = (['pause'] if item['state'] != 'investigating' else []), item['state']
    elif runtime['ready_label'] in labels(issue):
        state, detail, actions, case = 'queued', 'Queued for Forge.', ['backlog', 'pause'], 'queued'
    else:
        actions = ['queue']
    if state == 'queued' and item.get('retry_after', 0) > time.time():
        account = item.get('reason') in ('blocked_auth', 'blocked_quota', 'Role credentials unavailable')
        detail = ('Waiting for a coding account. Forge will retry automatically.' if account
                  else 'Waiting before another check. Forge will retry automatically.')
        case = 'waiting_account' if account else 'waiting_check'
    if state == 'queued' and not runtime['enabled']:
        detail, case = 'Forge is paused. This issue stays queued.', 'paused'
    if not fresh:
        actions = []
    if not repo.get('enabled') or not repo.get('access_ready'):
        actions = []
        if state not in ('completed', 'working'):
            state, detail, case = 'attention', 'Repository access or activation needs attention.', 'repo_blocked'
    return {'status': state, 'explanation': detail, 'summary': summary_for(case, item, held), 'actions': actions, 'setup_needed': setup or not repo.get('access_ready'),
            'environment_blocker': (item.get('environment_hold') or {}).get('detail'),
            'pull_requests': [{'number': p['number'], 'title': p.get('title', '')} for p in linked],
            'dependencies': dependencies(issue.get('body')), 'fresh': fresh,
            'reviewers_requested': {k: v for k, v in (item.get('reviewers_requested') or {}).items()
                                    if k in ('logins', 'at')} or None}


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
                    if (not force and old.get('complete') and not old.get('error') and not old.get('scan')
                            and 0 <= self.clock() - old.get('at', 0) < 60):
                        continue
                    entry = deepcopy(old)
                    entry.update(identity=identity, name=repo['name'], refreshing=True, error=None)
                    scan = entry.setdefault('scan', {'state': 'open', 'page': 1, 'issues': {}})
                    item = {'repo': repo['name'], 'repo_id': repo['id']}
                    # Who may approve here changes far more slowly than the
                    # issues do, and a failed listing only costs the select.
                    if not 0 <= self.clock() - (entry.get('collaborators') or {}).get('at', 0) < COLLABORATOR_AGE:
                        try:
                            rows = self.github.collaborators(item)
                        except (GitHubUnavailable, ValueError, KeyError, TypeError, OSError):
                            rows = []
                        entry['collaborators'] = {'at': self.clock(), 'rows': rows}
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
                                   for role, value in (item or {}).get('evidence', {}).items() if role in ('review', 'audit')],
                       'reviewers': (entry.get('collaborators') or {}).get('rows', []),
                       'default_reviewers': list(self.runtime['human_reviewers'])}
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
        return {'issues': [{k: v for k, v in r.items() if k not in ('body', 'history', 'reviews', 'reviewers', 'default_reviewers', 'reviewers_requested')} for r in rows[(page-1)*50:page*50]],
                'total': len(rows), 'page': page, 'pages': max(1, (len(rows)+49)//50), 'repositories': scopes,
                'complete': all(r['complete'] and r['fresh'] for r in scopes if repo_id is None or r['id'] == repo_id), 'actions': self.action_statuses()}

    def action_statuses(self):
        with AtomicStore(self.root / 'state/issues', 'actions').transaction() as data:
            ordered = sorted(data.items(), key=lambda pair: pair[1]['created_at'])
            recent = {key for key, value in ordered[-20:]}
            return [{'id': key, 'key': value['key'], 'status': value['status'], 'message': value.get('message')}
                    for key, value in ordered if value['status'] == 'pending' or key in recent]
