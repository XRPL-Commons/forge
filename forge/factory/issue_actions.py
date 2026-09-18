"""Durable issue decisions. Executed only under the supervisor's controller lease."""
from copy import deepcopy
from pathlib import Path
import time
from urllib.parse import quote
import uuid

from ..settings import Settings
from ..state import AtomicStore, StateError
from .config import load
from .contracts import references_issue
from .github import GitHub, GitHubUnavailable
from .issues import IssueInventory, facts, labels, revision, dependencies, LEGACY_SETUP_REASON

ACTIONS = {'queue', 'backlog', 'pause', 'resume', 'retry', 'recheck'}


def request(root, *, key, action, revision: str, reason):
    if not isinstance(action, str) or action not in ACTIONS or not isinstance(key, str) or not isinstance(revision, str) or not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise ValueError('Choose an issue action and record the decision.')
    inventory = IssueInventory(root)
    row = inventory.view(key=key)['issue']
    if row['revision'] != revision or action not in row['actions']:
        raise ValueError('This issue changed or needs a fresh check. Review it again before continuing.')
    store = AtomicStore(Path(root) / 'state/issues', 'actions')
    with store.transaction() as data:
        for identifier, value in data.items():
            if value['key'] == key and value['status'] == 'pending':
                if (value['action'], value['revision']) == (action, revision):
                    return identifier
                raise ValueError('Another decision for this issue is still being applied.')
        identifier = str(uuid.uuid4())
        data[identifier] = {'key': key, 'repo_id': row['repo_id'], 'repo': row['repo'], 'number': row['number'],
                            'action': action, 'revision': revision, 'reason': reason.strip(),
                            'status': 'pending', 'created_at': time.time()}
    return identifier


class IssueActions:
    def __init__(self, root, ledger, *, settings=None, github=None):
        self.root, self.ledger = Path(root), ledger
        self.settings = settings or Settings(root)
        self.config = load(root)
        self.github = github or GitHub(self.settings, self.config)
        self.store = AtomicStore(self.root / 'state/issues', 'actions')

    def process(self):
        with self.store.transaction() as data:
            pending = [(key, deepcopy(value)) for key, value in data.items() if value['status'] == 'pending'][:5]
        if not pending:
            return
        self.github.deadline = time.monotonic() + 45
        for identifier, decision in pending:
            try:
                if self.settings.paused():
                    raise ValueError('Forge is resetting. No issue decision was applied.')
                self.apply(identifier, decision)
                status, message = 'applied', 'Decision applied. Forge will recheck eligibility before starting work.'
            except GitHubUnavailable:
                status, message = 'pending', 'GitHub is unavailable. Forge will confirm this decision before starting work.'
            except (ValueError, StateError, KeyError, OSError) as error:
                status, message = 'rejected', str(error) if isinstance(error, ValueError) else 'Could not confirm the decision. Refresh and review the issue.'
                with self.ledger.transaction() as state:
                    item = state['issues'].get(decision['key'])
                    if item and item.get('operator_request') == identifier:
                        item.pop('operator_request', None)
                        item.setdefault('operator_hold', {'kind': 'conflict', 'state': item['state'] if item['state'] in ('ready', 'built', 'reviewed', 'audited') else 'ready'})
                        item.update(state='hold', reason='An issue decision changed while being applied. Review before resuming.')
                        self.ledger.event(state, item, 'operator_conflict', reason=message, request=identifier)
            with self.store.transaction() as data:
                data[identifier].update(status=status, message=message)
        IssueInventory(self.root).start_refresh(force=True)

    def prerequisites(self, decision, issue, item):
        held = labels(issue).intersection(self.config['hold_labels'])
        if held - {'hold'}:
            raise ValueError('Resolve the dependency label before releasing this hold.')
        for number in dependencies(issue.get('body')):
            related = self.github.call('controller', decision, 'GET', f'/issues/{number}')
            if related.get('number') != number or related.get('state') != 'closed':
                raise ValueError(f'Issue #{number} must be completed first.')
        owned = (item.get('candidate') or {}).get('pr') if item else None
        pulls = self.github.pulls(decision)
        if any(p['number'] != owned and references_issue(p.get('body'), decision['repo'], decision['number']) for p in pulls):
            raise ValueError('An existing pull request needs review before Forge starts another attempt.')
        if not owned and (issue.get('assignees') or 'wip' in labels(issue)):
            raise ValueError('Existing work or an assignee needs review before Forge starts another attempt.')
        if not self.settings.admission(decision['repo'])['allowed']:
            raise ValueError('Repository access must be verified first.')
        if not self.github.protections(decision, {'base_ref': self.github.target(decision)}):
            raise ValueError('Repository review rules must be verified first.')

    def apply(self, identifier, decision):
        data = self.ledger.read()
        if any(e.get('request') == identifier for e in data['events']):
            with self.ledger.transaction() as state:
                item = state['issues'].get(decision['key'])
                if item and item.get('operator_request') == identifier:
                    item.pop('operator_request', None)
            return
        repo = next((r for r in self.settings.view()['repositories'] if r['id'] == decision['repo_id'] and r['name'] == decision['repo'] and r['enabled']), None)
        if not repo or not repo.get('access_ready'):
            raise ValueError('Repository identity or access changed.')
        item = data['issues'].get(decision['key'])
        if item and (item['active'] or any(j['issue_key'] == decision['key'] and not j['cleanup'] for j in data['jobs'].values())):
            raise ValueError('Wait for the active attempt and its cleanup before changing the queue.')
        value, requirements = self.github.issue(decision)
        issue = facts(value)
        if issue['state'] != 'open':
            raise ValueError('This issue is closed.')
        plan = decision.get('plan')
        if plan is None:
            if revision(issue, item) != decision['revision']:
                raise ValueError('This issue changed. Refresh it before applying the decision.')
            self.validate_action(decision['action'], issue, item)
            add, remove = [], []
            if decision['action'] == 'pause':
                add = ['hold']
            elif decision['action'] == 'backlog':
                remove = [self.config['ready_label']]
            elif decision['action'] in ('queue', 'resume', 'retry'):
                self.prerequisites(decision, value, item)
                add = [self.config['ready_label']]
                if decision['action'] == 'resume':
                    remove = ['hold']
            elif decision['action'] == 'recheck':
                self.prerequisites(decision, value, item)
                if labels(issue).intersection(self.config['hold_labels']):
                    raise ValueError('An explicit hold requires a separate release decision.')
            after = deepcopy(issue)
            after['labels'] = sorted((labels(issue) | set(add)) - set(remove))
            plan = {'add': add, 'remove': remove, 'before': issue, 'new_item': item is None,
                    'after_revision': revision(after, item), 'ledger_revision': revision(issue, item)}
            # Persist intent before touching GitHub; a restarted controller can
            # recognize the intended label changes without duplicating decisions.
            with self.store.transaction() as requests:
                requests[identifier]['plan'] = plan
        else:
            compare_item = None if plan.get('new_item') else item
            normalized, before = deepcopy(issue), deepcopy(plan['before'])
            touched = set(plan['add'] + plan['remove'])
            normalized['labels'] = sorted(labels(normalized) - touched)
            before['labels'] = sorted(labels(before) - touched)
            if revision(normalized, compare_item) != revision(before, compare_item) or revision(plan['before'], compare_item) != decision['revision']:
                raise ValueError('Issue state changed while applying the earlier decision.')
            self.validate_action(decision['action'], plan['before'], compare_item)
            if decision['action'] in ('queue', 'resume', 'retry', 'recheck'):
                self.prerequisites(decision, value, item)
        if item is None:
            self.ledger.discover(repo, decision['number'], requirements, hold='Applying operator decision', title=value.get('title'))
        with self.ledger.transaction() as state:
            state['issues'][decision['key']]['operator_request'] = identifier
        for name in plan['add']:
            if name not in labels(issue):
                self.github.call('controller', decision, 'POST', f"/issues/{decision['number']}/labels", {'labels': [name]})
        for name in plan['remove']:
            if name in labels(issue):
                self.github.call('controller', decision, 'DELETE', f"/issues/{decision['number']}/labels/{quote(name, safe='')}")
        confirmed, requirements = self.github.issue(decision)
        if revision(facts(confirmed), None if plan.get('new_item') else item) != plan['after_revision']:
            raise ValueError('GitHub changed during this decision. Review the current issue.')
        if decision['action'] in ('queue', 'resume', 'retry', 'recheck'):
            self.prerequisites(decision, confirmed, item)
        if decision['action'] == 'retry':
            self.ledger.retry(decision['key'], decision['reason'], request_id=identifier)
            with self.ledger.transaction() as state:
                state['issues'][decision['key']].pop('operator_request', None)
                state['issues'][decision['key']]['github_observation'] = {'at': time.time(), 'issue': facts(confirmed)}
            return
        if decision['action'] == 'recheck':
            self.ledger.migrate_protection_hold(decision['key'])
            if not self.ledger.read()['issues'][decision['key']].get('prerequisite_hold'):
                raise ValueError('This hold cannot be confirmed as a repository setup hold. Review it before releasing.')
        with self.ledger.transaction() as state:
            current = state['issues'][decision['key']]
            if current['active']:
                raise ValueError('An attempt started while this decision was pending.')
            if decision['action'] in ('pause', 'backlog'):
                current['operator_hold'] = {'kind': decision['action'], 'state': current['state'] if current['state'] in ('ready', 'built', 'reviewed', 'audited') else 'ready'}
                current.update(state='hold', reason='Paused by an operator' if decision['action'] == 'pause' else 'Not queued for Forge')
                current.pop('prerequisite_hold', None)
            else:
                previous = current.get('operator_hold') or current.get('prerequisite_hold') or {}
                current.update(state=previous.get('state', 'ready'), reason=None)
                current.pop('operator_hold', None)
                current.pop('prerequisite_hold', None)
            current.pop('operator_request', None)
            current['github_observation'] = {'at': time.time(), 'issue': facts(confirmed)}
            self.ledger.event(state, current, 'operator_' + decision['action'], reason=decision['reason'], request=identifier)

    def validate_action(self, action, issue, item):
        state = (item or {}).get('state')
        held = labels(issue).intersection(self.config['hold_labels'])
        if action == 'retry' and state != 'failed':
            raise ValueError('Retry is only for failed attempts.')
        if action in ('queue', 'backlog', 'pause') and (held or state in ('failed', 'merged', 'approval_needed', 'investigating')):
            raise ValueError('Resolve the existing decision before changing the queue.')
        if action == 'queue' and state == 'hold' and not (item or {}).get('operator_hold', {}).get('kind') == 'backlog':
            raise ValueError('This hold needs its own release decision.')
        if action == 'resume' and ((state not in ('hold', 'cancelled') and item is not None) or not (held or (item or {}).get('operator_hold') or state == 'cancelled')):
            raise ValueError('There is no operator hold to release.')
        if action == 'recheck' and not ((item or {}).get('prerequisite_hold') or (item or {}).get('reason') == LEGACY_SETUP_REASON):
            raise ValueError('There is no repository setup hold to recheck.')
