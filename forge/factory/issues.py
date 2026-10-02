"""Read-only issue inventory and operator-facing decisions, separate from admission."""
from copy import deepcopy
from pathlib import Path
import re
import threading
import time

from ..settings import Settings, read_json, write_json
from ..state import AtomicStore
from .config import load
from .contracts import HEADLINE_LIMIT, MAX_FAILURES, claims_issue, clamp, digest, plain_text, dependencies
from .github import GitHub, GitHubUnavailable
from .ledger import Ledger
from .ledger import APPROACH_HOLD
from .ownership import forge_branch, ownership

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
            'assignees': [a['login'] for a in issue.get('assignees', [])],
            'assignee_types': {a['login']: a.get('type') for a in issue.get('assignees', [])}}


def revision(issue, item):
    public = {k: issue.get(k) for k in ('number', 'title', 'content_hash', 'state', 'assignees', 'assignee_types')}
    public['labels'] = sorted(l for l in labels(issue) if not l.startswith('forge:'))
    tracked = {k: (item or {}).get(k) for k in ('state', 'active', 'candidate', 'budget_start', 'operator_hold', 'prerequisite_hold', 'environment_hold', 'decision_request', 'environment_override', 'environment_choice', 'environment_retry')}
    if (item or {}).get('decision_version'):
        tracked['decision_version'] = item['decision_version']
    return digest({'github': public, 'ledger': tracked})


def decision_labels(issue, item):
    """Project the accepted local decision until GitHub confirms its labels."""
    sync = (item or {}).get('operator_sync')
    if not sync:
        return issue
    return {**issue, 'labels': sorted((labels(issue) | set(sync['add'])) - set(sync['remove']))}



# Plain language for the issue page: one headline sentence and at most three
# short sentences, for a reader who does not read code. Forge writes them for
# every state a worker has not spoken for; {why} carries the reason the issue
# is where it is, so nobody has to open Details to learn it.
SUMMARIES = {
    'decision': ('Waiting for your decision',
                 'The builder needs your answer before work can continue. Discuss the options, then retry with your decision.'),
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
    'dependencies': ('Waiting for prerequisite issues',
                     '{why} Forge checks again automatically and starts when they are closed.'),
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
WORKER_LED = ('failed', 'environment_hold', 'decision')


def sentence(text):
    text = ' '.join(str(text or '').split())
    return text if not text or text[-1] in '.!?…' else text + '.'


def forge_summary(case, why=''):
    headline, plain = SUMMARIES[case]
    # The reason comes second, so the clamp never drops it for Forge's own words.
    return {'headline': clamp(headline, HEADLINE_LIMIT),
            'plain': plain_text(plain.format(why=sentence(plain_text(why, 200, 2))).replace('  ', ' ').strip())}


def why_for(case, item):
    """The reason this issue is where it is, in the words Forge already has."""
    if case == 'dependencies':
        return item.get('dependency_reason') or ''
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


def reviewers(entry, configured):
    """Known people who can approve here, each with where Forge learned of them."""
    rows, seen = [], set()
    sources = ([(r, 'collaborator') for r in (entry.get('collaborators') or {}).get('rows', [])]
               + [({'login': login, 'name': ''}, 'recent_author') for login in (entry.get('collaborators') or {}).get('authors', [])]
               + [({'login': login, 'name': ''}, 'configured') for login in configured])
    for row, source in sources:
        login = str(row.get('login') or '')
        if login and login.lower() not in seen:
            seen.add(login.lower())
            rows.append({'login': login, 'name': row.get('name') or '', 'source': source})
    return rows


def attempts(item):
    """Charged failures since the budget last reset, against the limit."""
    failures = (item or {}).get('failures') or []
    start = (item or {}).get('budget_start') or 0
    return {'failed': sum(1 for f in failures[start:] if f.get('charged', True)), 'limit': MAX_FAILURES}


def presentation(issue, item, repo, runtime, prs=(), *, fresh=True):
    item = item or {}
    held = labels(issue).intersection(runtime['hold_labels'])
    if item.get('operator_sync') and (item.get('operator_hold') or {}).get('kind') == 'pause':
        held.discard('hold')  # The local pause is effective before its GitHub label exists.
    number = issue['number']
    # Work on this issue is a PR that claims it, not one that mentions it.
    linked = [p for p in prs if forge_branch(p, number) or claims_issue(p.get('body'), repo['name'], number)]
    owned_pr = (item.get('candidate') or {}).get('pr')
    gate = item.get('merge_gate') or {}
    if (not gate.get('candidate') or gate.get('candidate') != (item.get('candidate') or {}).get('key')
            or item.get('state') not in ('audited', 'approval_needed')):
        gate = {}
    merge_blocked = gate.get('kind') in ('blocked', 'refused')
    audit = (item.get('evidence') or {}).get('audit') or {}
    # Existing ledgers predate merge_gate. Only the exact human-request reason
    # is sufficient; approval_needed also historically held technical blockers.
    legacy_approval = not gate and not item.get('active') and bool(item.get('reason')) and (
        item.get('reason') == 'An authorized human must approve the current candidate'
        or audit.get('human_required') == 'required' and item.get('reason') == audit.get('human_reason'))
    human_approval = gate.get('kind') == 'approval' or legacy_approval
    # Forge's own earlier drafts are not someone else's work.
    owner = ownership(issue, item, repo['name'], prs)
    if owned_pr and not any(p['number'] == owned_pr for p in linked):
        linked.append({'number': owned_pr, 'title': 'Forge pull request'})
    kind = lambda p: 'candidate' if p['number'] == owned_pr else 'forge_attempt' if forge_branch(p, number) else 'external'
    state, detail, actions, case = 'backlog', 'Not queued for Forge.', [], 'backlog'
    setup = bool(item.get('prerequisite_hold')) or item.get('reason') == LEGACY_SETUP_REASON
    cleanup = bool(item.get('cleanup_pending'))
    if issue['state'] == 'closed' or item.get('state') == 'merged':
        merged = item.get('state') == 'merged'
        state, detail, case = 'completed', 'Completed and merged.' if merged else 'Closed on GitHub.', 'merged' if merged else 'closed'
    elif item.get('operator_request'):
        state, detail, case = 'waiting', 'Applying your decision. Forge will confirm it before continuing.', 'applying'
    elif item.get('active') and not (item.get('active_role') == 'merge' and (owner or gate.get('kind') in ('approval', 'blocked', 'refused'))):
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
    elif item.get('decision_request'):
        state, detail, actions, case = 'attention', item['decision_request']['question'], ['retry'], 'decision'
    elif setup:
        state = 'attention'
        ready = repo.get('protection_ready') and repo.get('access_ready')
        detail = 'Repository setup now passes. Recheck to resume.' if ready else 'Repository setup needs attention.'
        actions, case = (['recheck'] if ready else []), ('setup_ready' if ready else 'setup')
    elif item.get('state') == 'failed':
        state, detail, actions, case = 'attention', item.get('reason') or 'The last attempt failed.', ['retry'], 'failed'
    elif owner:
        state, detail, case = 'waiting', owner['plain'], 'external'
    elif item.get('state') == 'cancelled':
        state, detail, actions, case = 'attention', 'The previous attempt was stopped. Review before resuming.', ['resume'], 'cancelled'
    elif item.get('operator_hold'):
        backlog = item['operator_hold']['kind'] == 'backlog'
        state, detail, actions = ('backlog', 'Not queued for Forge.', ['queue']) if backlog else ('attention', 'Paused by an operator.', ['resume'])
        case = 'backlog' if backlog else 'operator_hold'
    elif item.get('state') == 'hold':
        state, detail, case = 'attention', item.get('reason') or 'An operator decision is needed.', 'hold'
        if item.get('reason') == APPROACH_HOLD:
            detail += ' Describe the new approach on the issue, then retry with that decision.'
            actions, case = ['retry'], 'approach_hold'
    elif merge_blocked:
        state, detail, case = 'attention', gate.get('reason') or 'GitHub has not allowed this merge.', 'merge_blocked'
    elif item.get('state') == 'approval_needed':
        state, detail, case = 'attention', 'The pull request needs a human review.', 'approval_needed'
        # Asking a named human for that review is the only decision on offer;
        # the approval itself still has to reach Forge through GitHub.
        actions = ['assign_reviewer'] if owned_pr else []
    elif item.get('blocked_dependencies'):
        state, detail, case = 'waiting', item.get('dependency_reason'), 'dependencies'
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
    summary = ({'headline': 'The merge is blocked', 'plain': detail} if case == 'merge_blocked'
               else owner if case == 'external' else summary_for(case, item, held))
    return {'status': state, 'explanation': detail, 'summary': summary, 'actions': actions, 'setup_needed': setup or not repo.get('access_ready'),
            'approval': {'pr': owned_pr, 'reason': gate.get('reason') or item.get('reason') or 'Review the pull request changes.'}
                        if case == 'approval_needed' and owned_pr and human_approval else None,
            'approvals': [{k: a.get(k) for k in ('login', 'state', 'at', 'counted', 'why')} for a in gate.get('approvals') or []],
            'last_checked': item.get('last_observed'),
            'merge_blocker': {'reason': detail} if case == 'merge_blocked' else None,
            'environment_blocker': (item.get('environment_hold') or {}).get('detail'),
            'decision_request': item.get('decision_request'),
            'environment': {'choice': item.get('environment_choice'), 'override': item.get('environment_override'),
                            'full_available': 'full' in runtime.get('environment_tiers', {}),
                            'editable': bool(fresh and repo.get('enabled') and repo.get('access_ready')
                                             and issue['state'] == 'open' and item.get('state') != 'merged'
                                             and not item.get('active') and not cleanup and not item.get('operator_request'))},
            'pull_requests': [{'number': p['number'], 'title': p.get('title', ''), 'kind': kind(p)} for p in linked],
            'attempts': attempts(item) if attempts(item)['failed'] else None,
            'held_by': [h for h in (issue.get('label_events') or []) if h.get('label') in held] or None,
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
                        try:
                            authors = self.github.recent_authors(item)
                        except (GitHubUnavailable, ValueError, KeyError, TypeError, OSError):
                            authors = []
                        entry['collaborators'] = {'at': self.clock(), 'rows': rows, 'authors': authors}
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
                                    previous = (old.get('issues') or {}).get(str(row['number'])) or {}
                                    if set(row['labels']) & set(self.runtime['hold_labels']):
                                        # Who put a hold here is the first question; keep it until labels change.
                                        if previous.get('labels') == row['labels'] and 'label_events' in previous:
                                            row['label_events'] = previous['label_events']
                                        else:
                                            try:
                                                row['label_events'] = self.github.label_events(item, row['number'])
                                            except (GitHubUnavailable, ValueError, KeyError, TypeError, OSError):
                                                row['label_events'] = []
                                    scan['issues'][str(row['number'])] = row
                            entry['issues'] = {**old.get('issues', {}), **scan['issues']}
                            entry.update(at=self.clock(), complete=False)
                            if len(batch) < 100:
                                if scan['state'] == 'open':
                                    scan.update(state='closed', page=1)
                                else:
                                    entry['pulls'] = [{'number': p['number'], 'title': p.get('title', ''), 'body': (p.get('body') or '')[:262144],
                                                       'branch': (p.get('head') or {}).get('ref'), 'author': (p.get('user') or {}).get('login'),
                                                       'forge_owned': p.get('forge_owned', False)}
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
                issue = decision_labels(issue, item)
                # A rescan runs every minute; an issue it has not reached yet is still
                # known from the last complete one. Actions carry the revision and are
                # checked under the ledger lock; GitHub is verified before dispatch.
                ready = fresh and (str(issue['number']) in entry.get('scan', {}).get('issues', {})
                                   or str(issue['number']) in entry.get('issues', {}))
                row = {**issue, 'key': issue_key, 'repo': repo['name'], 'repo_id': repo['id'],
                       **presentation(issue, item, repo, self.runtime, entry.get('pulls', []), fresh=ready),
                       'revision': revision(issue, item), 'setup_url': repo['setup_url'],
                       'history': [{k: e[k] for k in ('at', 'kind', 'reason') if k in e} for e in ledger['events'] if e['issue'] == issue_key][-15:],
                       'active': item.get('active') if item else None,
                       'reviews': [{'role': role, 'outcome': value.get('outcome'), 'summary': value.get('summary', '')[:1000],
                                    # The audit records 'required' / 'not_required': a word, not a flag.
                                    'human_required': value.get('human_required') == 'required',
                                    'human_reason': str(value.get('human_reason') or '')[:2000] or None}
                                   for role, value in (item or {}).get('evidence', {}).items() if role in ('review', 'audit')],
                       'reviewers': reviewers(entry, self.runtime['human_reviewers']),
                       'default_reviewers': list(self.runtime['human_reviewers'])}
                if key is not None:
                    if key == issue_key:
                        # A receipt is news until the issue moves on; after that it only misleads.
                        latest = max((e['at'] for e in row['history']), default=0)
                        actions = [a for a in self.action_statuses() if a['status'] == 'pending' or a['key'] != issue_key
                                   or (a.get('at') or 0) >= latest]
                        return {'issue': row, 'repositories': scopes, 'actions': actions}
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
            return [{'id': key, 'key': value['key'], 'status': value['status'], 'message': value.get('message'),
                     'at': value.get('created_at')}
                    for key, value in ordered if value['status'] == 'pending' or key in recent]
