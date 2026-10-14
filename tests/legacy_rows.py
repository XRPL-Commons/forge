"""Seed SQLite rows shaped like a unit imported from the legacy JSON ledger.

The JSON importer is gone, but imported units still exist on the one
production store (they wait behind the `legacy_reconciliation` gate). These
fixtures insert the same rows the importer produced so the code that
reconciles, recovers and displays those units stays covered until it is
removed.
"""
import hashlib
import json
from pathlib import Path
import tempfile
import zlib

from forge.store.configuration import Configuration
from forge.store.db import Store
from forge.store.presentation import record_approval_display, record_label_display, record_revision_display
from forge.store.remote_time import normalize as remote_time, order as remote_order
from forge.store.transitions import manifest_hash, project_unit

LEDGER = 'state/factory/factory-ledger.json'
MANIFEST = 'manifest'
PHASE = {'ready': 'queued', 'built': 'reviewing', 'reviewed': 'auditing', 'audited': 'merging', 'approval_needed': 'merging'}
CONTEXT_FIELDS = [('summary_json', 'last_summary'), ('retry_plan_json', 'retry_plan'),
                  ('decision_request_json', 'decision_request'), ('verification_request_json', 'verification_request')]
ENVIRONMENT_FIELDS = [('choice_json', 'environment_choice'), ('override_json', 'environment_override'),
                      ('retry_json', 'environment_retry'), ('memory_retry_json', 'memory_retry'), ('environment_hold_json', 'environment_hold')]


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


def text(value):
    return encode(value).decode() if value is not None else None


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def insert(db, table, **values):
    db.execute(f"INSERT INTO {table}({','.join(values)}) VALUES({','.join('?' for _ in values)})", tuple(values.values()))


def gate(db, unit, code, at, details=None, candidate=None):
    insert(db, 'unit_gates', id=unit+':gate:'+code, unit_id=unit, candidate_id=candidate,
           subject_key='candidate:'+candidate if candidate else 'unit', code=code, created_at=at,
           details_json=text(details or {}))


def artifact(db, job, kind, value, at):
    raw = encode(value)
    for sequence, start in enumerate(range(0, len(raw), 1024*1024)):
        part = raw[start:start+1024*1024]
        packed = zlib.compress(part)
        codec, content = ('zlib', packed) if len(packed) < len(part) else ('identity', part)
        insert(db, 'job_artifacts', job_id=job, kind=kind, sequence=sequence, media_type='application/json',
               digest=digest(part), content=content, size=len(content), raw_size=len(part), codec=codec, created_at=at)


def failure_count(item):
    return sum(f['charged'] for f in item['failures'][item['budget_start']:])


def seed(destination, ledger, *, settings=None, inventory=None):
    """Write the imported projection of a cleaned legacy ledger into a new store."""
    store = Store.create(destination)
    settings = settings or {}
    if settings:
        Configuration(store).replace('factory.json', settings, expected_revision=0)
    inventory = inventory or {'repositories': {}}
    configured = {r['id']: r for r in settings.get('repositories', [])}
    repositories = {int(k): v['name'] for k, v in inventory.get('repositories', {}).items()}
    repositories.update({r['id']: r['name'] for r in settings.get('repositories', []) if r.get('name')})
    repositories.update({item['repo_id']: item['repo'] for item in ledger['issues'].values()})
    observed = {}
    for key, item in ledger['issues'].items():
        observation = item.get('github_observation') or {}
        fresh = observation.get('issue')
        old = observed.get(key)
        if fresh and (old is None or (remote_order(fresh.get('updated_at')), observation.get('at') or 0) >= (remote_order(old[1].get('updated_at')), old[2])):
            observed[key] = (item['repo_id'], fresh, observation.get('at') or 0)
        elif old is None:
            observed[key] = (item['repo_id'], dict(number=item['number'], title=item.get('title', ''), body='', state='open'), item['created_at'])
    merges = {}
    for key, item in ledger['issues'].items():
        receipt = (item.get('evidence') or {}).get('merge') or {}
        if receipt.get('sha') and receipt.get('pr') == (item.get('candidate') or {}).get('pr'):
            merges[key] = receipt['sha']
    for event in ledger['events']:
        if event.get('kind') == 'merged' and event.get('sha'):
            merges[event['issue']] = event['sha']
    completed_at = {e['attempt']: e['at'] for e in ledger['events'] if e.get('kind') == 'runner_released' and e.get('attempt')}

    def add_unit(db, key, item, revision):
        unit = 'legacy:unit:'+key
        original = state = item['state']
        if state in ('hold', 'failed', 'investigating'):
            saved = [h['state'] for field in ('operator_hold', 'environment_hold', 'prerequisite_hold')
                     if isinstance(h := item.get(field), dict) and h.get('state') in PHASE]
            state = saved[0] if len(set(saved)) == 1 else None
        phase = PHASE.get(state, 'queued')
        verification = item.get('verification_request')
        if original == 'approval_needed' and verification and verification.get('resume_state') in ('built', 'reviewed'):
            phase = PHASE[verification['resume_state']]
        merge_sha = merges.get(key)
        if original == 'merged' and merge_sha:
            phase = 'merged'
        close_code = None
        if original == 'cancelled':
            facts = observed[key][1]
            if facts.get('state') == 'closed' and facts.get('state_reason') in ('completed', 'not_planned', 'duplicate'):
                phase, close_code = 'closed', 'github_'+facts['state_reason']
        candidate = item.get('candidate') or {}
        build = ledger['jobs'].get(candidate.get('build_attempt'), {})
        base = candidate.get('base_ref') or build.get('target_branch') or 'main'
        complete = build and all(candidate.get(k) for k in ('pr', 'head', 'base', 'policy', 'requirements', 'key'))
        insert(db, 'units', id=unit, repository_id=item['repo_id'], base_branch=base, state=phase, close_code=close_code,
               merge_sha=merge_sha if phase == 'merged' else None,
               current_candidate_id='legacy:candidate:'+candidate['key'] if complete else None,
               version=max(1, item.get('decision_version', 0)), budget_generation=2 if item['budget_start'] else 1,
               created_at=item['created_at'], updated_at=item.get('last_observed') or item['created_at'])
        terminal = phase in ('merged', 'closed')
        insert(db, 'unit_issues', id='legacy:member:'+key, unit_id=unit, issue_id=key, repository_id=item['repo_id'],
               base_branch=base, requirements_revision_id=revision,
               state='done' if phase == 'merged' else 'removed' if terminal else 'pending', joined_at=item['created_at'],
               left_at=(item.get('settled_observed') or item['created_at']) if terminal else None,
               exit_reason=phase if terminal else None)
        if not terminal:
            gate(db, unit, 'legacy_reconciliation', item['created_at'], {'source': LEDGER, 'issue_key': key, 'legacy_state': original})
            for field, code in [('operator_hold', 'operator_backlog' if (item.get('operator_hold') or {}).get('kind') == 'backlog' else 'operator_pause'),
                                ('prerequisite_hold', 'repository_setup'), ('environment_hold', 'environment'),
                                ('label_hold', 'label_hold'), ('operator_sync', 'operator_sync'),
                                ('ownership_wait', 'ownership_conflict'), ('blocked_dependencies', 'dependency'),
                                ('decision_request', 'decision_required')]:
                if item.get(field):
                    gate(db, unit, code, item['created_at'], {'legacy_field': field, 'value': item[field]})
            if failure_count(item) >= 3:
                gate(db, unit, 'budget_exhausted', item['created_at'])
            if item.get('retry_after'):
                gate(db, unit, 'retry_delay', item['created_at'], {'retry_at': item['retry_after']})
        if complete:
            assignment = {k: v for k, v in build.items() if k not in ('capture', 'observation', 'ci_evidence')}
            insert(db, 'candidates', id='legacy:candidate:'+candidate['key'], unit_id=unit, pull_number=candidate['pr'],
                   branch=candidate.get('branch') or build['branch'], head_oid=candidate['head'], base_oid=candidate['base'],
                   policy_hash=candidate['policy'], requirements_hash=candidate['requirements'], membership_generation=1,
                   input_manifest_hash=manifest_hash(assignment), accepted_at=candidate.get('accepted_at') or item['created_at'])
        if not terminal:
            current = 'legacy:candidate:'+candidate['key'] if complete else None
            if verification and current and verification.get('candidate') == candidate.get('key'):
                gate(db, unit, 'verification_ci' if verification.get('ci') else 'verification_human',
                     verification.get('created_at') or item['created_at'], verification, candidate=current)
            merge = item.get('merge_gate')
            if merge and merge.get('kind') != 'ready' and current and merge.get('candidate') == candidate.get('key'):
                gate(db, unit, 'approval_required' if merge.get('kind') == 'approval' else 'merge_checks',
                     item['created_at'], merge, candidate=current)
            if original == 'investigating' or any(f.get('investigation') == 'pending' for f in item['failures']):
                gate(db, unit, 'recovery_pending', item['created_at'], {'source': LEDGER})

    with store.write() as db:
        # Imported rows bypass the live transition triggers, as the importer did.
        db.execute("INSERT INTO store_metadata VALUES('import_only','true')")
        db.execute("INSERT INTO store_metadata VALUES('source_manifest',?)", (MANIFEST,))
        db.execute("INSERT INTO import_runs VALUES(?,'fixture',?,1,'validating',NULL,0,0)", (MANIFEST, 'a'*40))
        for repo_id, name in sorted(repositories.items()):
            insert(db, 'repositories', id=repo_id, full_name=name, enabled=int(configured.get(repo_id, {}).get('enabled', False)))
        for key, (repo, facts, at) in sorted(observed.items()):
            item = ledger['issues'].get(key)
            body = facts.get('body') or ''
            requirement_hash = facts.get('content_hash') or manifest_hash(body)
            revision = 'legacy:revision:'+digest(encode([key, body, requirement_hash]))
            labels = [v['name'] if isinstance(v, dict) else v for v in facts.get('labels', [])]
            assignees = [v['login'] if isinstance(v, dict) else v for v in facts.get('assignees', [])]
            insert(db, 'issues', id=key, repository_id=repo, github_number=facts['number'], github_state=facts.get('state', 'open'),
                   state_reason=facts.get('state_reason'), current_revision_id=revision,
                   remote_updated_at=remote_time(facts['updated_at']) if facts.get('updated_at') is not None else None,
                   observed_at=at, labels_json=text(labels), assignees_json=text(assignees),
                   assignee_types_json=text(facts.get('assignee_types', {})))
            insert(db, 'issue_revisions', id=revision, issue_id=key, title=facts.get('title', ''), body=body,
                   content_hash=manifest_hash([facts.get('title', ''), body]), requirements_hash=requirement_hash,
                   requirements_json=text({'legacy_body_hash': requirement_hash, 'body_truncated': bool(facts.get('body_truncated'))}),
                   observed_at=at)
            record_revision_display(db, revision, body, {'body_truncated': bool(facts.get('body_truncated'))}, source_content_hash=requirement_hash)
            record_label_display(db, key, labels, facts.get('label_events', []))
            if item:
                pinned, pinned_revision = item.get('requirements'), revision
                if pinned and pinned != requirement_hash:
                    pinned_revision = 'legacy:pinned:'+key
                    insert(db, 'issue_revisions', id=pinned_revision, issue_id=key, title=item.get('title', ''), body='',
                           content_hash=manifest_hash([item.get('title', ''), '']), requirements_hash=pinned,
                           requirements_json=text({'legacy_body_hash': pinned, 'body_unavailable': True}), observed_at=item['created_at'])
                    record_revision_display(db, pinned_revision, '', {'body_unavailable': True}, source_content_hash=pinned)
                add_unit(db, key, item, pinned_revision)
        for identifier, job in sorted(ledger['jobs'].items()):
            key = job['issue_key']; item = ledger['issues'][key]; unit = 'legacy:unit:'+key
            index = next((i for i, f in enumerate(item['failures']) if f['attempt'] == identifier), None)
            generation = (1 if index is not None and index < item['budget_start'] else 2) if item['budget_start'] else 1
            assignment = {k: v for k, v in job.items() if k not in ('capture', 'observation', 'ci_evidence')}
            manifest = {'legacy_job_id': identifier, 'source_manifest': MANIFEST, 'assignment_digest': manifest_hash(assignment)}
            insert(db, 'attempts', id=identifier, unit_id=unit, member_id='legacy:member:'+key, membership_generation=1,
                   assignment_generation=1, budget_generation=generation, input_manifest_json=text(manifest), created_at=job['created_at'])
            insert(db, 'jobs', id=identifier, unit_id=unit, attempt_id=identifier, member_id='legacy:member:'+key, role=job['role'],
                   phase='cleaned', outcome=job['outcome'], fenced=1, cleanup=1, created_at=job['created_at'])
            artifact(db, identifier, 'launch_input', assignment, job['created_at'])
            capture = job.get('capture') or {}
            artifact(db, identifier, 'result', {k: v for k, v in capture.items() if k != 'logs'}, job['created_at'])
            finished = completed_at.get(identifier, job['created_at'])
            if 'logs' in capture:
                artifact(db, identifier, 'logs', capture['logs'], finished)
            for kind, field in (('live_tail', 'observation'), ('ci_transcript', 'ci_evidence')):
                if job.get(field):
                    artifact(db, identifier, kind, job[field], finished)
            if identifier not in completed_at:
                for row in db.execute("SELECT kind,sequence FROM job_artifacts WHERE job_id=? AND kind IN ('logs','live_tail','ci_transcript')", (identifier,)).fetchall():
                    insert(db, 'artifact_pins', job_id=identifier, kind=row['kind'], sequence=row['sequence'],
                           purpose='legacy_completion_time_unknown', reference_id=identifier)
            if index is not None:
                failure = item['failures'][index]
                insert(db, 'budget_entries', id=identifier+':charge', attempt_id=identifier, kind='charge', reason_code='legacy_charge', created_at=failure['at'])
                if not failure['charged']:
                    insert(db, 'budget_entries', id=identifier+':correction', attempt_id=identifier, kind='correction',
                           corrects_id=identifier+':charge', reason_code='legacy_correction', created_at=failure['at'])
            observation = job.get('observation') or {}
            insert(db, 'job_public_metadata', job_id=identifier, slot=job.get('slot'), provider=job.get('provider'),
                   provider_id=job.get('provider_id'), model=job.get('model'), deadline=job.get('deadline'),
                   observed_at=observation.get('at'), started_at=(observation.get('execution') or {}).get('started_at'),
                   source_phase=job.get('phase'), source_fenced=job.get('fenced'))
        for key, item in sorted(ledger['issues'].items()):
            unit = 'legacy:unit:'+key; candidate = item.get('candidate') or {}
            current = db.execute('SELECT current_candidate_id FROM units WHERE id=?', (unit,)).fetchone()[0]
            if current:
                merge = item.get('merge_gate') or {}
                if merge.get('candidate') == candidate.get('key') and merge.get('approvals'):
                    record_approval_display(db, current, merge['approvals'], item.get('last_observed') or item['created_at'], source='legacy')
                for role, value in item.get('evidence', {}).items():
                    attempt = value.get('attempt')
                    at = ledger['jobs'][attempt]['created_at'] if attempt else candidate.get('accepted_at') or item['created_at']
                    insert(db, 'candidate_evidence', id=current+':'+role, unit_id=unit, candidate_id=current, attempt_id=attempt,
                           kind=role, verdict='pass', actor_id='legacy-controller', observed_at=at, details_json=text(value))
            insert(db, 'unit_context', unit_id=unit, reason=item.get('reason'), checks_settled=item.get('checks_settled'),
                   last_observed_at=item.get('last_observed'), settled_observed=item.get('settled_observed'),
                   observation_interruptions=item.get('budget_interruptions', 0),
                   **{column: text(item.get(field)) for column, field in CONTEXT_FIELDS})
            insert(db, 'issue_environment_observations', issue_id=key, label=item.get('environment_label'),
                   **{column: text(item.get(field)) for column, field in ENVIRONMENT_FIELDS})
            insert(db, 'dependency_observations', unit_id=unit, requirements_body=item.get('dependency_body'),
                   evidence=item.get('dependency_evidence'), reason=item.get('dependency_reason'), checked_at=item.get('dependencies_checked_at'))
            for kind, field in (('blocked', 'blocked_dependencies'), ('declared', 'declared_dependencies')):
                for position, member in enumerate(item.get(field, [])):
                    insert(db, 'dependency_observation_members', unit_id=unit, kind=kind, position=position, value_json=text(member))
            for position, failure in enumerate(item['failures']):
                insert(db, 'attempt_failures', attempt_id=failure['attempt'], unit_id=unit, position=position, role=failure.get('role'),
                       cause=failure.get('cause'), investigation=failure.get('investigation'), retry_stage=failure.get('retry_state'),
                       evidence_kind='failure_evidence', findings_json=text(failure.get('findings')),
                       summary_json=text(failure.get('summary')), at=failure['at'])
                artifact(db, failure['attempt'], 'failure_evidence', failure.get('evidence'), failure['at'])
            reviewers = item.get('reviewers_requested')
            if reviewers and current and item.get('state') in {'audited', 'approval_needed', 'merged'}:
                insert(db, 'candidate_reviewer_requests', candidate_id=current, request_id=reviewers.get('request') or 'legacy:'+key,
                       actor_id='legacy:operator', logins_json=text(reviewers['logins']), requested_at=reviewers['at'], issue_assignment=1)
        for event in ledger['events']:
            key, sequence = event.get('issue'), event['sequence']
            payload = {'legacy_kind': event['kind'], 'source': LEDGER, 'source_record': ['events', sequence-1],
                       'digest': digest(encode(event)), 'legacy': event}
            insert(db, 'events', sequence=sequence, transaction_id='legacy:event:'+str(sequence),
                   unit_id='legacy:unit:'+key if key else None, issue_id=key, kind='legacy_event', at=event['at'], payload_json=text(payload))
        for key in ledger['issues']:
            view = project_unit(db, 'legacy:unit:'+key)
            primary = view['current_blocker']
            db.execute('UPDATE units SET primary_gate_id=? WHERE id=?', (primary['id'] if primary else None, view['id']))
    return store


class LegacyLedger:
    """Test mixin: one cleaned legacy issue, ready to seed as imported rows."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.issue = dict(key='1:1', repo_id=1, repo='test/repo', number=1, title='Change', state='ready', reason=None,
                          requirements='requirements-hash', candidate=None, active=None, failures=[], budget_start=0, created_at=10)
        self.ledger = dict(version=1, issues={'1:1': self.issue}, jobs={}, slots={},
                           events=[dict(sequence=1, at=10, issue='1:1', kind='discovered')])
        self.settings = {'repositories': [{'id': 1, 'enabled': True}]}

    def seed(self):
        return seed(self.base/'staging', self.ledger, settings=self.settings)
