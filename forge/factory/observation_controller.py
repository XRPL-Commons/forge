"""Ordered GitHub observations for every current SQLite delivery member.

Collection generations are reserved before network reads. A late response cannot
replace a newer observation. All issue facts, holds and readiness changes commit
with one lifecycle command; imported reconciliation gates are never auto-cleared.
"""
import time
import uuid

from ..store.db import StoreError
from ..store.models import Command, TransitionError
from ..store.queries import Queries
from ..store.transitions import Lifecycle, TERMINAL
from .github import GitHubUnavailable
from ..request_metrics import RequestBudgetExhausted, RequestMetrics, request_scope, deadline
from .ownership import ownership, active_publication_matches


class ObservationController:
    def __init__(self, store, github, *, admission, policy=None,
                 hold_labels=('hold', 'blocked'), clock=time.time, monotonic=time.monotonic):
        self.store, self.github, self.admission = store, github, admission
        self.policy, self.hold_labels, self.clock = policy, tuple(hold_labels), clock
        self.monotonic = monotonic
        self.commands = Lifecycle(store, clock=clock)
        self.queries = Queries(store)

    def _snapshot(self, unit_id, member_id):
        unit = self.queries.unit(unit_id)
        if unit['state'] in TERMINAL:
            return None
        member=next((row for row in unit['members'] if row['id']==member_id),None)
        if member is None:
            return None
        issue=self.queries.issue(member['issue_id'])
        with self.store.read_snapshot() as db:
            if db.execute("SELECT 1 FROM store_metadata WHERE key='import_only' AND value='true'").fetchone():
                raise TransitionError('staging imports cannot observe live GitHub state')
            selected = db.execute('SELECT * FROM candidates WHERE id=?', (unit['current_candidate_id'],)).fetchone()
        candidate = dict(selected) if selected else None
        item = dict(repo=issue['repository'], repo_id=issue['repository_id'], number=issue['github_number'],
                    candidate={'pr': candidate['pull_number'],'key':candidate['id']} if candidate else None,unit_state=unit['state'])
        from ..store.operator_views import ownership_context
        item=ownership_context(self.store,item,unit['current_candidate_id'])
        from .ownership import publication_context
        return unit, issue, candidate, publication_context(self.store,unit_id,item)

    def _generation(self, issue):
        key = 'observation_generation:' + issue['id']
        with self.store.write() as db:
            if db.execute("SELECT 1 FROM store_metadata WHERE key='import_only' AND value='true'").fetchone():
                raise TransitionError('staging imports cannot reserve observations')
            row = db.execute('SELECT value FROM store_metadata WHERE key=?', (key,)).fetchone()
            current = db.execute('SELECT collection_generation FROM issues WHERE id=?', (issue['id'],)).fetchone()[0]
            generation = max(int(row[0]) if row else 0, current) + 1
            db.execute('INSERT INTO store_metadata(key,value) VALUES(?,?) '
                       'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(generation)))
        return generation

    @staticmethod
    def _command(unit):
        return Command(str(uuid.uuid4()), 'supervisor:observation', unit['id'], unit['version'])

    def _external_change(self, unit, detail):
        with self.store.read_snapshot() as db:
            existing = db.execute("SELECT 1 FROM unit_gates WHERE unit_id=? AND code='external_change' "
                                  'AND resolved_at IS NULL', (unit['id'],)).fetchone()
        if not existing:
            self.commands.open_gate(self._command(unit), 'external_change', details={'reason': detail})
        return {'unit_id': unit['id'], 'pending': 'external_change'}

    def step(self, unit_id):
        unit=self.queries.unit(unit_id)
        if unit['state'] in TERMINAL:
            return {'unit_id':unit_id,'terminal':True}
        outcomes={member['id']:self.observe_member(unit_id,member['id'],member['issue_id'])
                  for member in unit['members']}
        if len(outcomes)==1:
            return next(iter(outcomes.values()))
        return {'unit_id':unit_id,'members':outcomes}

    def observe_member(self, unit_id, member_id, issue_id):
        with self.store.read_snapshot() as db:
            requested=db.execute('SELECT requested_at FROM operator_observation_requests WHERE issue_id=?',(issue_id,)).fetchone()
        outcome=self._step_member(unit_id,member_id)
        if requested and not outcome.get('stale') and not outcome.get('pending'):
            with self.store.write() as db:
                db.execute('DELETE FROM operator_observation_requests WHERE issue_id=? AND requested_at=?',(issue_id,requested[0]))
        return outcome

    def _step_member(self, unit_id, member_id):
        snapshot = self._snapshot(unit_id, member_id)
        if snapshot is None:
            return {'unit_id': unit_id, 'terminal': True}
        unit, issue, candidate, item = snapshot
        with self.store.read_snapshot() as db:
            if db.execute("SELECT 1 FROM unit_gates WHERE unit_id=? AND code='legacy_reconciliation' AND resolved_at IS NULL", (unit_id,)).fetchone():
                return {'unit_id': unit_id, 'pending': 'legacy_reconciliation'}
        generation = self._generation(issue)
        remote, requirements = self.github.issue(item)
        if (remote.get('number') != item['number'] or 'pull_request' in remote
                or not isinstance(remote.get('updated_at'), str)):
            raise GitHubUnavailable('Issue observation identity or ordering is unverified')
        labels = [row['name'] if isinstance(row, dict) else row for row in remote.get('labels', [])]
        assignees = [row['login'] if isinstance(row, dict) else row for row in remote.get('assignees', [])]
        remote_pr, pr_error = None, None
        if candidate:
            try:
                remote_pr = self.github.pr(item, candidate['pull_number'])
            except (GitHubUnavailable, RequestBudgetExhausted) as error:
                pr_error = error
        terminal = remote['state'] == 'closed' or remote_pr is not None and remote_pr.get('merged') is True
        # A verified closure must fence its worker even when unrelated inventory
        # or candidate reads are unavailable. It cannot clear ownership/readiness.
        conflict = allowed = None
        if not terminal and pr_error is None:
            pulls = [pr for pr in self.github.pulls(item)
                     if not active_publication_matches(self.store, unit_id, pr)]
            conflict = ownership(remote, item, item['repo'], pulls) is not None
            allowed = self.admission(item)
            if type(allowed) is not bool:
                raise TransitionError('admission adapter must return a verified boolean')
        outcome = self.commands.observe_issue(self._command(unit), issue['id'], state=remote['state'],
            state_reason=remote.get('state_reason'), title=remote['title'], body=remote.get('body') or '',
            requirements=requirements, remote_updated_at=remote['updated_at'], generation=generation,
            labels=labels, assignees=assignees, hold_labels=self.hold_labels,
            admission=allowed, ownership_conflict=conflict,
            assignee_types={row['login']: row['type'] for row in remote.get('assignees', [])
                            if isinstance(row, dict) and row.get('type')})
        if isinstance(pr_error, RequestBudgetExhausted):
            # Persist the already verified issue closure/holds before deferring
            # the unfinished candidate read. Keep this exact member pending.
            raise pr_error
        if outcome.get('stale'):
            return {'unit_id': unit_id, 'stale': True}
        current = self._snapshot(unit_id, member_id)
        if current is None:
            return {'unit_id': unit_id, 'terminal': True}
        unit, issue, selected, item = current
        # A requirements change may already have superseded the candidate.
        if candidate and (not selected or selected['id'] != candidate['id']):
            return {'unit_id': unit_id, 'invalidated': 'requirements'}
        proof = {'issue_version': issue['version']}
        if pr_error:
            raise pr_error
        if selected:
            pr = remote_pr if remote_pr is not None else self.github.pr(item, selected['pull_number'])
            if (pr.get('number') != selected['pull_number']
                    or (pr.get('head') or {}).get('repo', {}).get('id') != item['repo_id']
                    or (pr.get('base') or {}).get('repo', {}).get('id') != item['repo_id']):
                raise GitHubUnavailable('Candidate repository identity is unverified')
            exact_head = pr.get('head', {}).get('sha') == selected['head_oid']
            exact_base = pr.get('base', {}).get('sha') == selected['base_oid']
            if pr.get('merged') is True:
                if not exact_head or not exact_base:
                    return self._external_change(unit, 'merged_candidate_changed')
                with self.store.read_snapshot() as db:
                    busy = db.execute('SELECT 1 FROM jobs WHERE unit_id=? AND cleanup=0', (unit_id,)).fetchone()
                if busy:
                    return self._external_change(unit, 'external_merge_observed')
                merge_sha = pr.get('merge_commit_sha')
                return {'unit_id': unit_id, **self.commands.confirm_merged(self._command(unit), selected['id'],
                    merge_sha=merge_sha, evidence={'verified': True, 'merged': True, 'merge_sha': merge_sha,
                        'pull_number': selected['pull_number'], 'head_oid': selected['head_oid'],
                        'base_oid': selected['base_oid']})}
            if pr.get('merged') is not False:
                raise GitHubUnavailable('Candidate merge state is unverified')
            if remote['state'] == 'open' and active_publication_matches(self.store, unit_id, pr):
                return {'unit_id': unit_id, 'observed': True, 'publishing': True}
            if not exact_head:
                return self._external_change(unit, 'candidate_head_changed')
            proof.update(verified=True, merged=False, candidate_id=selected['id'],
                         pull_number=selected['pull_number'], head_oid=selected['head_oid'], observed_at=self.clock())
            if remote['state'] == 'open':
                if pr.get('state') != 'open' or pr.get('draft') is not False:
                    return self._external_change(unit, 'candidate_unavailable')
                if pr.get('base', {}).get('ref') != unit['base_branch']:
                    return self._external_change(unit, 'candidate_target_changed')
                if not exact_base or self.policy is not None and self.policy(item) != selected['policy_hash']:
                    reason = 'base_changed' if not exact_base else 'policy_changed'
                    return {'unit_id': unit_id, **self.commands.invalidate_candidate(self._command(unit),
                        selected['id'], reason=reason, evidence=proof)}
        if remote['state'] == 'closed' and remote.get('state_reason') in {'completed', 'not_planned', 'duplicate'}:
            if unit['delivery_approved']:
                with self.store.read_snapshot() as db:
                    existing=db.execute("SELECT 1 FROM unit_gates WHERE unit_id=? AND member_id=? AND code='membership_decision' AND resolved_at IS NULL",(unit_id,member_id)).fetchone()
                if not existing:
                    self.commands.open_gate(self._command(unit),'membership_decision',member_id=member_id,details={'reason':'member_closed','state_reason':remote['state_reason']})
                return {'unit_id':unit_id,'pending':'membership_decision'}
            return {'unit_id': unit_id, **self.commands.close_unit(self._command(unit),
                close_code='github_' + remote['state_reason'], evidence=proof)}
        return {'unit_id': unit_id, 'observed': True}

    def cycle(self, *, after=None, limit=50):
        # The durable sweep owns its checkpoint. `after` is the shared page
        # interface; a separate caller cursor cannot override this receipt.
        from ..store.queries import _limit
        from ..store.observation_progress import ObservationProgress
        limit=_limit(limit)
        progress=ObservationProgress(self.store)
        state=progress.start(self.clock())
        metrics=RequestMetrics();started=self.monotonic();previous=metrics.view()
        items=[];complete=False
        errors_before=state['errors'];phase_errors=set()
        def checkpoint():
            nonlocal previous,started
            current=metrics.view()
            for field in ('attempts','failures','elapsed_ms'):
                state['requests'][field]+=current[field]-previous[field]
            if current['quota']:
                state['requests']['quota']=current['quota']
            previous=current
            now=self.monotonic();state['active_ms']+=(now-started)*1000;started=now
            progress.save()
        with request_scope(metrics=metrics):
            while self.monotonic()<deadline():
                page=progress.page(state['cursor'],limit)
                if not page:
                    complete=True
                    break
                state['pages']+=1
                for member in page:
                    if self.monotonic()>=deadline():break
                    try:
                        outcome=self.observe_member(member['unit_id'],member['member_id'],member['issue_id'])
                    except RequestBudgetExhausted:
                        break
                    except (GitHubUnavailable,TransitionError,StoreError,OSError) as error:
                        outcome={'unit_id':member['unit_id'],'error':getattr(error,'code',type(error).__name__)}
                    state['visits']+=1
                    if outcome.get('error'):
                        state['errors']+=1
                        phase_errors.add(outcome['error'])
                        state['error_codes']=sorted(set(state['error_codes']+[outcome['error']]))[:10]
                    elif outcome.get('pending') or outcome.get('stale'):state['deferred']+=1
                    elif outcome.get('terminal'):state['retired']+=1
                    else:state['refreshed']+=1
                    state['cursor']={key:member[key] for key in ('unit_id','member_id','membership_generation')}
                    if len(items)<100:items.append({**outcome,'member_id':member['member_id']})
                    checkpoint()
                else:
                    continue
                break
        state['in_progress']=False
        checkpoint()
        if complete:
            summary=progress.finish(self.clock())
        else:
            state.update(coverage=progress.coverage(state['started_at'],self.clock()),
                         scope_changed=progress.scope_hash()!=state['scope_hash'],complete=False)
            progress.save()
            summary=state
        return {'items':items,'next_cursor':None if complete else state['cursor'],'checkpointed':True,
                'sweep':summary,'error_count':state['errors']-errors_before,'error_codes':sorted(phase_errors)[:10],
                'deferred':None if complete else 'github_budget'}
