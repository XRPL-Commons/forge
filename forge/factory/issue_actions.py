"""Immediate local decisions, with GitHub side effects owned by the supervisor."""
from copy import deepcopy
from pathlib import Path
import re
import time
from urllib.parse import quote
import uuid

from ..settings import Settings
from ..state import AtomicStore, StateError
from .config import load
from .contracts import claims_issue, issue_dependencies, digest
from .github import GitHub, GitHubUnavailable, own_build_pr
from .issues import IssueInventory, facts, labels, revision as issue_revision, decision_labels, dependencies, LEGACY_SETUP_REASON
from .ledger import APPROACH_HOLD, Ledger

ACTIONS = {'queue', 'backlog', 'pause', 'resume', 'retry', 'recheck', 'start_anyway', 'assign_reviewer', 'environment', 'close'}
IMMEDIATE = {'queue', 'backlog', 'pause', 'resume', 'retry', 'recheck', 'start_anyway'}
LOGIN = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})')


def decide(root, *, key, action, revision: str, reason, request_id=None, actor_id=None):
    """Accept a decision without a GitHub call or waiting for the controller lease."""
    from .store_operator import selected_service
    service=selected_service(root,actor_id=actor_id)
    if service is not None:return service.request(key,action,revision,reason,request_id=request_id)
    if not isinstance(action, str) or action not in IMMEDIATE:
        return request(root, key=key, action=action, revision=revision, reason=reason, request_id=request_id)
    if not isinstance(key, str) or not isinstance(revision, str) or not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise ValueError('Choose an issue action and record the decision.')
    identifier = str(uuid.uuid4()) if request_id is None else request_id
    try:
        uuid.UUID(identifier)
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Invalid decision identity') from None
    inventory = IssueInventory(root)
    ledger = Ledger(Path(root) / 'state/factory')
    service = IssueActions(root, ledger, settings=inventory.settings, github=inventory.github)
    decision = {'key': key, 'action': action, 'revision': revision, 'reason': reason.strip()}
    # Legacy requests and reviewer/environment writes still have external work.
    with service.store.transaction() as requests:
        if any(r['key'] == key and r['status'] == 'pending' for r in requests.values()):
            raise ValueError('Another decision for this issue is still being applied.')
    row = inventory.view(key=key)['issue']
    with ledger.transaction() as data:
        old = next((e for e in data['events'] if e.get('request') == identifier and e.get('decision')), None)
        if old:
            if old['decision'] != decision:
                raise ValueError('Decision identity was already used')
            return identifier
        item = data['issues'].get(key)
        if inventory.settings.paused():
            raise ValueError('Forge is resetting. No issue decision was applied.')
        if item and (item['active'] or item.get('operator_request') or any(
                j['issue_key'] == key and (not j['cleanup'] or j.get('outcome') is None) for j in data['jobs'].values())):
            raise ValueError('Wait for the active attempt and its cleanup before changing the queue.')
        if row['revision'] != revision or issue_revision(row, item) != revision or action not in row['actions']:
            raise ValueError('This issue changed or needs a fresh check. Review it again before continuing.')
        service.validate_action(action, row, item)
        if action == 'recheck' and labels(row).intersection(service.config['hold_labels']):
            raise ValueError('An explicit hold requires a separate release decision.')
        if action == 'recheck' and item.get('reason') == LEGACY_SETUP_REASON:
            if not ledger.migrate_protection_hold_in(data, key):
                raise ValueError('This hold cannot be confirmed as a repository setup hold. Review it before releasing.')
        if item is None:
            ledger.discover_in(data, {'id': row['repo_id'], 'name': row['repo']}, row['number'], None, title=row['title'])
            item = data['issues'][key]
        add, remove = [], []
        if action == 'pause':
            add = ['hold']
        elif action == 'backlog':
            remove = [service.config['ready_label']]
        elif action in ('queue', 'resume', 'retry', 'start_anyway'):
            add = [service.config['ready_label']]
            if action == 'resume':
                remove = ['hold']
        # Compose successive decisions even if GitHub has not observed the first.
        previous = item.get('operator_sync') or {}
        adds = (set(previous.get('add', [])) | set(add)) - set(remove)
        removes = (set(previous.get('remove', [])) | set(remove)) - set(add)
        stage = (item.get('operator_hold') or item.get('environment_hold') or item.get('prerequisite_hold') or {}).get('state', 'ready')
        if action in ('pause', 'backlog'):
            stage = item['state'] if item['state'] in ('ready', 'built', 'reviewed', 'audited') else stage
            item['operator_hold'] = {'kind': action, 'state': stage}
            item.update(state='hold', reason='Paused by an operator' if action == 'pause' else 'Not queued for Forge')
        else:
            item.update(state='ready' if action == 'retry' else stage, reason=None, retry_plan=reason.strip())
            item.pop('operator_hold', None)
            if action != 'retry' and item.get('verification_request'):
                item.update(state='approval_needed', reason=item['verification_request']['what'])
        if action == 'retry':
            item.update(budget_start=len(item['failures']), evidence={})
            item.pop('verification_request', None)
            item.pop('verified_checks', None)
            answer = item.pop('decision_request', None)
            if answer:
                ledger.event(data, item, 'decision_answered', decision=answer, reason=reason.strip())
        if action == 'start_anyway':
            cleared_dependencies = item['declared_dependencies'][:]
            for field in ('declared_dependencies', 'dependency_body', 'dependency_evidence', 'dependencies_checked_at'):
                item.pop(field, None)
        for field in ('prerequisite_hold', 'environment_hold', 'blocked_dependencies', 'dependency_reason'):
            item.pop(field, None)
        item.update(retry_after=0, last_observed=0, decision_version=item.get('decision_version', 0) + 1,
                    operator_sync={'request': identifier, 'action': action, 'add': sorted(adds), 'remove': sorted(removes)})
        ledger.event(data, item, 'human_retry' if action == 'retry' else 'operator_' + action,
                     reason=reason.strip(), request=identifier, decision=decision,
                     **({'issues': cleared_dependencies} if action == 'start_anyway' else {}))
    return identifier


def reviewer_logins(reason):
    """The decision for assign_reviewer is the chosen logins, nothing else."""
    logins = []
    for value in re.split(r'[\s,;]+', (reason or '').strip()):
        name = value.lstrip('@')
        if not name:
            continue
        if not LOGIN.fullmatch(name):
            raise ValueError('Choose GitHub logins for the reviewers.')
        if name.lower() not in [l.lower() for l in logins]:
            logins.append(name)
    if not 1 <= len(logins) <= 10:
        raise ValueError('Choose between one and ten reviewers.')
    return logins


def request(root, *, key, action, revision: str, reason, request_id=None, environment=None, actor_id=None):
    from .store_operator import selected_service
    service=selected_service(root,actor_id=actor_id)
    if service is not None:return service.request(key,action,revision,reason,request_id=request_id,environment=environment)
    if action == 'start_anyway' and environment is None:
        return decide(root, key=key, action=action, revision=revision, reason=reason, request_id=request_id)
    if not isinstance(action, str) or action not in ACTIONS or not isinstance(key, str) or not isinstance(revision, str) or not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise ValueError('Choose an issue action and record the decision.')
    if action == 'environment':
        from .environments import override
        environment = override(environment)
    elif environment is not None:
        raise ValueError('An environment belongs to an environment decision.')
    if action == 'assign_reviewer':
        reviewer_logins(reason)
    if request_id is not None:
        try: uuid.UUID(request_id)
        except (ValueError, TypeError, AttributeError): raise ValueError('Invalid decision identity') from None
        with AtomicStore(Path(root) / 'state/issues', 'actions').transaction() as data:
            old = data.get(request_id)
            if old:
                if (old['key'], old['action'], old['revision'], old['reason'], old.get('environment')) != (key, action, revision, reason.strip(), environment):
                    raise ValueError('Decision identity was already used')
                return request_id
    inventory = IssueInventory(root)
    row = inventory.view(key=key)['issue']
    allowed = (row.get('environment', {}).get('editable', False) if action == 'environment'
               else row.get('closable', False) if action == 'close' else action in row['actions'])
    if row['revision'] != revision or not allowed:
        raise ValueError('This issue changed or needs a fresh check. Review it again before continuing.')
    store = AtomicStore(Path(root) / 'state/issues', 'actions')
    with store.transaction() as data:
        for identifier, value in data.items():
            if value['key'] == key and value['status'] == 'pending':
                if (value['action'], value['revision'], value['reason'], value.get('environment')) == (action, revision, reason.strip(), environment):
                    return identifier
                raise ValueError('Another decision for this issue is still being applied.')
        identifier = request_id or str(uuid.uuid4())
        data[identifier] = {'key': key, 'repo_id': row['repo_id'], 'repo': row['repo'], 'number': row['number'],
                            'action': action, 'revision': revision, 'reason': reason.strip(),
                            'status': 'pending', 'created_at': time.time(), **({'environment': environment} if action == 'environment' else {})}
    return identifier


class IssueActions:
    def __init__(self, root, ledger, *, settings=None, github=None):
        self.root, self.ledger = Path(root), ledger
        self.settings = settings or Settings(root)
        self.config = load(root)
        self.github = github or GitHub(self.settings, self.config)
        self.store = AtomicStore(self.root / 'state/issues', 'actions')

    def sync(self, item):
        """Confirm an immediate decision against live GitHub before admission."""
        pending = item['operator_sync']
        issue, requirements = self.github.issue(item)
        try:
            if issue['state'] != 'open':
                raise ValueError('Issue closed on GitHub.')
            repo = next((r for r in self.settings.view()['repositories'] if r['id'] == item['repo_id'] and r['name'] == item['repo']), None)
            if not repo or not repo.get('enabled') or not repo.get('access_ready'):
                raise ValueError('Repository identity or access changed.')
            if pending['action'] in ('queue', 'resume', 'retry', 'recheck', 'start_anyway'):
                self.prerequisites(item, decision_labels(issue, item), item)
            for name in pending['add']:
                if name not in labels(issue):
                    self.github.call('controller', item, 'POST', f"/issues/{item['number']}/labels", {'labels': [name]})
            for name in pending['remove']:
                if name in labels(issue):
                    self.github.call('controller', item, 'DELETE', f"/issues/{item['number']}/labels/{quote(name, safe='')}")
            confirmed, requirements = self.github.issue(item)
            if not set(pending['add']) <= labels(confirmed) or set(pending['remove']) & labels(confirmed):
                raise GitHubUnavailable('GitHub has not confirmed the issue labels.')
            if confirmed['state'] != 'open':
                raise ValueError('Issue closed on GitHub.')
            if pending['action'] in ('queue', 'resume', 'retry', 'recheck', 'start_anyway'):
                self.prerequisites(item, confirmed, item)
        except ValueError as error:
            with self.ledger.transaction() as data:
                current = data['issues'][item['key']]
                current['operator_hold'] = {'kind': 'conflict', 'state': current['state'] if current['state'] in ('ready', 'built', 'reviewed', 'audited') else 'ready'}
                current.update(state='hold', reason=str(error))
                current.pop('operator_sync', None)
                self.ledger.event(data, current, 'operator_conflict', reason=str(error))
            return
        with self.ledger.transaction() as data:
            current = data['issues'][item['key']]
            current.pop('operator_sync', None)
            if current['requirements'] is None:
                current['requirements'] = requirements
            current['github_observation'] = {'at': time.time(), 'issue': facts(confirmed)}
            self.ledger.event(data, current, 'operator_synced')

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
                with self.ledger.transaction() as state:
                    item = state['issues'].get(decision['key'])
                    if item and not item['active']:
                        item['operator_request'] = identifier
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
        for number in issue_dependencies(issue.get('body'), item):
            related = self.github.call('controller', decision, 'GET', f'/issues/{number}')
            if related.get('number') != number or related.get('state') != 'closed':
                raise ValueError(f'Issue #{number} must be completed first.')
        owned = (item.get('candidate') or {}).get('pr') if item else None
        pulls = self.github.pulls(decision)
        # Only a PR that says it resolves this issue is existing work; a passing
        # mention in another PR's description is not (the same rule as ownership).
        linked = [p for p in pulls if p['number'] != owned
                  and claims_issue(p.get('body'), decision['repo'], decision['number'])]
        if linked:
            publisher = self.github.publisher('build')
            other = [p for p in linked if not own_build_pr(p, decision['repo_id'], decision['number'], publisher)]
            if other:
                raise ValueError(f"Pull request #{other[0]['number']} says it resolves this issue. Review or close it "
                                 'before Forge starts another attempt.')
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
        if decision['action'] != 'close' and item and (item['active'] or any(j['issue_key'] == decision['key'] and not j['cleanup'] for j in data['jobs'].values())):
            raise ValueError('Wait for the active attempt and its cleanup before changing the queue.')
        value, requirements = self.github.issue(decision)
        issue = facts(value)
        if decision['action'] == 'close':
            # Replay-safe: a restarted controller finds the issue already closed and finishes.
            if issue['state'] == 'open' and issue_revision(issue, item) != decision['revision']:
                raise ValueError('This issue changed. Refresh it before applying the decision.')
            self.close(identifier, decision, item, value)
            return
        if issue['state'] != 'open':
            raise ValueError('This issue is closed.')
        plan = decision.get('plan')
        if plan is None:
            if issue_revision(issue, item) != decision['revision']:
                raise ValueError('This issue changed. Refresh it before applying the decision.')
            self.validate_action(decision['action'], issue, item)
            add, remove = [], []
            if decision['action'] == 'environment':
                from .environments import override
                selected = override(decision.get('environment'))
                if selected['mode'] == 'full' and 'full' not in self.config['environment_tiers']:
                    raise ValueError('Database workers are unavailable on this host.')
                remove = sorted(label for label in labels(issue) if label.startswith('env:'))
            elif decision['action'] == 'pause':
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
                    'after_revision': issue_revision(after, item), 'ledger_revision': issue_revision(issue, item)}
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
            if decision['action'] == 'assign_reviewer':
                # The assignees this decision writes are its own effect, never
                # a conflict when a restarted controller replays it.
                normalized['assignees'] = before['assignees'] = []
                normalized['assignee_types'] = before['assignee_types'] = {}
            if issue_revision(normalized, compare_item) != issue_revision(before, compare_item) or issue_revision(plan['before'], compare_item) != decision['revision']:
                raise ValueError('Issue state changed while applying the earlier decision.')
            self.validate_action(decision['action'], plan['before'], compare_item)
            if decision['action'] in ('queue', 'resume', 'retry', 'recheck'):
                self.prerequisites(decision, value, item)
        if decision['action'] == 'environment' and plan.get('new_item') and self.config['ready_label'] in labels(issue):
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
        if decision['action'] == 'assign_reviewer':
            self.assign_reviewer(identifier, decision, item)
            return
        confirmed, requirements = self.github.issue(decision)
        if issue_revision(facts(confirmed), None if plan.get('new_item') else item) != plan['after_revision']:
            raise ValueError('GitHub changed during this decision. Review the current issue.')
        if decision['action'] in ('queue', 'resume', 'retry', 'recheck'):
            self.prerequisites(decision, confirmed, item)
        if decision['action'] == 'environment':
            from .environments import override
            selected = override(decision.get('environment'))
            if plan.get('new_item') and self.config['ready_label'] in labels(confirmed):
                self.prerequisites(decision, confirmed, item)
            if selected['mode'] == 'full' and 'full' not in self.config['environment_tiers']:
                raise ValueError('Database workers are unavailable on this host.')
            with self.ledger.transaction() as state:
                current = state['issues'][decision['key']]
                for field in ('environment_choice', 'environment_label', 'environment_label_pending', 'environment_retry'):
                    current.pop(field, None)
                if selected['mode'] == 'automatic':
                    current.pop('environment_override', None)
                else:
                    current['environment_override'] = selected
                if plan.get('new_item'):
                    queued = self.config['ready_label'] in labels(confirmed)
                    current.update(state='ready' if queued else 'hold', reason=None if queued else 'Not queued for Forge')
                    if not queued:
                        current['operator_hold'] = {'kind': 'backlog', 'state': 'ready'}
                current.pop('operator_request', None)
                current['retry_after'] = 0
                current['github_observation'] = {'at': time.time(), 'issue': facts(confirmed)}
                self.ledger.event(state, current, 'environment_override', reason=decision['reason'],
                                  environment=selected, request=identifier)
            return
        if decision['action'] == 'retry':
            self.ledger.retry(decision['key'], decision['reason'], request_id=identifier)
            with self.ledger.transaction() as state:
                state['issues'][decision['key']].pop('operator_request', None)
                state['issues'][decision['key']]['github_observation'] = {'at': time.time(), 'issue': facts(confirmed)}
            return
        if decision['action'] == 'recheck':
            self.ledger.migrate_protection_hold(decision['key'])
            if not any(self.ledger.read()['issues'][decision['key']].get(k) for k in ('prerequisite_hold', 'environment_hold')):
                raise ValueError('This hold cannot be confirmed as a repository setup hold. Review it before releasing.')
        with self.ledger.transaction() as state:
            current = state['issues'][decision['key']]
            if current['active']:
                raise ValueError('An attempt started while this decision was pending.')
            if decision['action'] in ('pause', 'backlog'):
                current['operator_hold'] = {'kind': decision['action'], 'state': current['state'] if current['state'] in ('ready', 'built', 'reviewed', 'audited') else 'ready'}
                current.update(state='hold', reason='Paused by an operator' if decision['action'] == 'pause' else 'Not queued for Forge')
                current.pop('prerequisite_hold', None)
                current.pop('environment_hold', None)
            else:
                previous = current.get('operator_hold') or current.get('environment_hold') or current.get('prerequisite_hold') or {}
                current.update(state=previous.get('state', 'ready'), reason=None)
                if current.get('verification_request'):
                    current.update(state='approval_needed', reason=current['verification_request']['what'])
                current.pop('operator_hold', None)
                current.pop('prerequisite_hold', None)
                current.pop('environment_hold', None)
            if decision['action'] in ('queue', 'resume', 'recheck'):
                current['retry_plan'] = decision['reason']
            current.pop('operator_request', None)
            current['github_observation'] = {'at': time.time(), 'issue': facts(confirmed)}
            self.ledger.event(state, current, 'operator_' + decision['action'], reason=decision['reason'], request=identifier)

    def close(self, identifier, decision, item, github_issue):
        """Drop the issue: close Forge's pull request, then the issue as not planned.

        Stops a running attempt; the next cycle cleans up its runner. Branches are kept."""
        pull = ((item or {}).get('candidate') or {}).get('pr')
        if pull:
            current = self.github.call('controller', decision, 'GET', f'/pulls/{pull}')
            if isinstance(current, dict) and current.get('state') == 'open' and not current.get('merged'):
                self.github.call('controller', decision, 'PATCH', f'/pulls/{pull}', {'state': 'closed'})
        if github_issue['state'] == 'open':
            self.github.call('controller', decision, 'POST', f"/issues/{decision['number']}/comments",
                             {'body': 'Closed from Forge as not planned: ' + decision['reason']})
            self.github.call('controller', decision, 'PATCH', f"/issues/{decision['number']}",
                             {'state': 'closed', 'state_reason': 'not_planned'})
            github_issue = {**github_issue, 'state': 'closed', 'state_reason': 'not_planned'}
        if item is None:
            return
        self.ledger.cancel(decision['key'], 'Closed as not planned: ' + decision['reason'], github_issue=github_issue)
        with self.ledger.transaction() as state:
            current = state['issues'][decision['key']]
            current.pop('operator_request', None)
            current['ownership_wait'] = None
            self.ledger.event(state, current, 'operator_close', reason=decision['reason'], request=identifier)

    def assign_reviewer(self, identifier, decision, item):
        """Hand the candidate to a human through GitHub's own review request.

        No label and no state change: the issue keeps waiting for the approval
        that GitHub.human() alone accepts.
        """
        logins = reviewer_logins(decision['reason'])
        pull = (item.get('candidate') or {})['pr']
        self.github.call('controller', decision, 'POST', f'/pulls/{pull}/requested_reviewers', {'reviewers': logins})
        self.github.call('controller', decision, 'POST', f"/issues/{decision['number']}/assignees", {'assignees': logins})
        confirmed, _ = self.github.issue(decision)
        with self.ledger.transaction() as state:
            current = state['issues'][decision['key']]
            current['reviewers_requested'] = {'logins': logins, 'at': time.time(), 'request': identifier}
            current.pop('operator_request', None)
            current['github_observation'] = {'at': time.time(), 'issue': facts(confirmed)}
            self.ledger.event(state, current, 'reviewer_requested', reason=', '.join(logins), request=identifier)

    def validate_action(self, action, issue, item):
        state = (item or {}).get('state')
        held = labels(issue).intersection(self.config['hold_labels'])
        if action == 'start_anyway' and (not item or state != 'ready' or held
                or not item.get('declared_dependencies') or not item.get('blocked_dependencies')
                or issue.get('body_truncated')
                or item.get('dependency_body') != (issue.get('content_hash') or digest(issue.get('body') or ''))
                or set(dependencies(issue.get('body'))) & set(item['blocked_dependencies'])):
            raise ValueError('Only worker-declared prerequisites can be cleared here. Edit explicit dependencies on GitHub.')
        if action == 'retry' and not ((state == 'approval_needed' and (item or {}).get('verification_request')) or state == 'failed' or (state == 'hold' and (
                (item or {}).get('reason') == APPROACH_HOLD or (item or {}).get('decision_request')))):
            raise ValueError('Retry is only for failed attempts or a hold that waits for a new approach.')
        if action in ('queue', 'backlog', 'pause') and (held or state in ('failed', 'merged', 'approval_needed', 'investigating')):
            raise ValueError('Resolve the existing decision before changing the queue.')
        if action == 'queue' and state == 'hold' and not (item or {}).get('operator_hold', {}).get('kind') == 'backlog':
            raise ValueError('This hold needs its own release decision.')
        if action == 'resume' and ((state not in ('hold', 'cancelled') and item is not None) or not (held or (item or {}).get('operator_hold') or state == 'cancelled')):
            raise ValueError('There is no operator hold to release.')
        if action == 'recheck' and not ((item or {}).get('prerequisite_hold') or (item or {}).get('environment_hold') or (item or {}).get('reason') == LEGACY_SETUP_REASON):
            raise ValueError('There is no repository setup hold to recheck.')
        if action == 'assign_reviewer' and not (state == 'approval_needed' and ((item or {}).get('candidate') or {}).get('pr')):
            raise ValueError('A reviewer can only be assigned while a pull request waits for approval.')
