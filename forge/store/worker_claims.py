"""Typed worker claims and exact, bounded opportunities to test them again.

A report cannot establish an external fact. Claims preserve their original job
and member pins; only their own obligations release, without resetting budgets.
"""
import json

from .models import TransitionError
from .transitions import Lifecycle, _charges, _id, _json, manifest_hash

REASONS = frozenset({'toolchain','sandbox','dependency_access','resources','ci_evidence','services','github_permission'})
RETRY_SECONDS = (300, 1800, 7200)


def _settings(db, domain):
    row = db.execute("SELECT settings_json FROM configuration WHERE domain=? AND subject='instance' AND active=1", (domain,)).fetchone()
    return json.loads(row[0]) if row else {}


def revisions_in(db, unit, job_id, reason):
    """Hash only relevant non-secret facts; observation/refresh times are not changes."""
    host, runtime = _settings(db, 'host.json'), _settings(db, 'runtime.json')
    launch = db.execute('SELECT provider,provider_id,runner_config_json FROM job_launches WHERE job_id=?', (job_id,)).fetchone()
    runner = json.loads(launch['runner_config_json']) if launch else {}
    members = list(db.execute('SELECT m.id,m.issue_id,m.requirements_revision_id FROM job_members p '
        'JOIN unit_issues m ON m.id=p.member_id WHERE p.job_id=? ORDER BY m.issue_id', (job_id,)))
    environment = []
    for member in members:
        selection = db.execute('SELECT choice,config_hash FROM unit_environment WHERE member_id=? ORDER BY revision DESC LIMIT 1', (member['id'],)).fetchone()
        override = db.execute('SELECT override_json,retry_json,memory_retry_json FROM issue_environment_observations WHERE issue_id=?', (member['issue_id'],)).fetchone()
        environment.append({'issue':member['issue_id'], 'choice':json.loads(selection['choice']) if selection else None,
            'policy':selection['config_hash'] if selection else None,
            'override':json.loads(override['override_json']) if override and override['override_json'] else None})
    release = {'source':host.get('source'), 'image':host.get('image'), 'full_image':host.get('full_image')}
    result = {'release':manifest_hash(release), 'environment':manifest_hash({'members':environment,
        'policy':{key:runtime.get(key) for key in ('image','environment_tiers','repository_environments','memory_tiers')}})}
    # The login generation changes on explicit connect, never token refresh.
    logins = _settings(db, 'provider-logins.json').get('generations', {})
    references = sorted(set(runner.get('provider_environment', [])) | ({runner['provider_auth_file_env']} if runner.get('provider_auth_file_env') else set()))
    result['provider_login'] = manifest_hash({reference:logins.get(reference) for reference in references})
    if reason in {'github_permission','dependency_access','ci_evidence'}:
        settings = _settings(db, 'factory.json')
        repository = next((row for row in settings.get('repositories',[]) if row.get('id')==unit['repository_id']), None)
        observed = db.execute('SELECT report_json FROM repository_setup_observations WHERE repository_id=?', (unit['repository_id'],)).fetchone()
        report = json.loads(observed[0]) if observed and observed[0] else {}
        result['repository'] = manifest_hash({'settings':repository, 'branch':unit['base_branch'],
            'protections':{key:report.get(key) for key in ('branch','available','requirements','additional_rules','code_owner_reviews','sources')}})
    return result


def identity_in(db, job, details):
    members = [dict(row) for row in db.execute('SELECT m.issue_id,r.requirements_hash FROM job_members p '
        'JOIN unit_issues m ON m.id=p.member_id JOIN issue_revisions r ON r.id=p.requirements_revision_id '
        'WHERE p.job_id=? ORDER BY m.issue_id', (job['id'],))]
    if not members:
        raise TransitionError('worker claim needs exact assigned member requirements')
    return manifest_hash({'reason':details['reason'],'services':sorted(details.get('required_services',[])),
                          'role':job['role'],'members':members})


def _legacy_jobs_in(db, gate):
    """Read old controller decisions, never infer a claim from prose or a timer."""
    details = json.loads(gate['details_json'])
    if details.get('source') == 'runtime_selection' or details.get('oom_killed'):
        return []
    identifiers = [details.get('job_id'), *[row.get('job_id') for row in details.get('worker_waits',[]) if isinstance(row,dict)]]
    result = []
    for identifier in dict.fromkeys(identifiers):
        row = db.execute("SELECT j.*,d.payload_json FROM jobs j JOIN decisions d ON json_extract(d.payload_json,'$.job_id')=j.id "
            "WHERE j.id=? AND j.outcome='deferred' AND d.command='defer_job' AND d.status='applied' "
            "AND json_extract(d.payload_json,'$.code')='environment' ORDER BY d.created_at LIMIT 1", (identifier,)).fetchone()
        if row:
            value = json.loads(row['payload_json'])['details']
            if value.get('reason') in REASONS and not value.get('oom_killed'):
                result.append((dict(row),value))
    return result


def eligible_gate_in(db, gate):
    return gate['code']=='environment' and (db.execute('SELECT 1 FROM worker_claim_obligations WHERE gate_id=?', (gate['id'],)).fetchone() is not None
                                           or bool(_legacy_jobs_in(db,gate)))


def hydrate_in(db, unit, gate, now):
    """Normalize historical accepted waits on first use, preserving every job pin."""
    for job, details in _legacy_jobs_in(db,gate):
        db.execute('INSERT OR IGNORE INTO worker_claim_attempts VALUES(?,?,?,?,?,?,?,?)',
            (job['id'],identity_in(db,job,details),details['reason'],_json(details),_json({'legacy':True}),'deferred',now,gate['created_at']))
        pins = db.execute('SELECT member_id FROM job_members WHERE job_id=? AND (? IS NULL OR member_id=?)',
                          (job['id'],gate['member_id'],gate['member_id'])).fetchall()
        for pin in pins:
            db.execute('INSERT OR IGNORE INTO worker_claim_obligations(gate_id,job_id,member_id) VALUES(?,?,?)', (gate['id'],job['id'],pin[0]))


class WorkerClaims(Lifecycle):
    def defer_claim(self, command, job_id, *, execution_fence, input_manifest, evidence, details):
        if (not isinstance(details,dict) or details.get('reason') not in REASONS or details.get('oom_killed')
                or not isinstance(details.get('required_services',[]),list)
                or set(details.get('required_services',[]))-{'mongodb','postgres','redis'}):
            raise TransitionError('worker claim needs a typed blocker and bounded services')
        payload = dict(job_id=job_id,execution_fence=execution_fence,input_manifest=input_manifest,evidence=evidence,details=details)
        def change(db,unit):
            job,current = self._job(db,unit,job_id,execution_fence,input_manifest)
            if not current:
                self._result(db,command,job,evidence,'stale_deferred')
                return {'accepted':False,'charged':False}
            if job['role'] not in {'build','integration','review','audit'}:
                raise TransitionError('worker claims require a pipeline assignment')
            key = identity_in(db,job,details)
            revisions = revisions_in(db,unit,job_id,details['reason'])
            previous = db.execute('SELECT c.* FROM worker_claim_attempts c WHERE claim_key=? AND revisions_json=? AND EXISTS(SELECT 1 FROM worker_claim_obligations o WHERE o.job_id=c.job_id AND o.released_at IS NOT NULL) ORDER BY created_at DESC LIMIT 1', (key,_json(revisions))).fetchone()
            now = self.clock()
            # Retest attempts remain bounded even when the worker varies its claim.
            count = db.execute('SELECT COUNT(*) FROM worker_claim_attempts c JOIN jobs j ON j.id=c.job_id '
                'WHERE j.unit_id=? AND j.role=? AND c.revisions_json=? AND EXISTS(SELECT 1 FROM worker_claim_obligations o WHERE o.job_id=c.job_id AND o.released_at IS NOT NULL) '
                'AND NOT EXISTS(SELECT member_id FROM job_members WHERE job_id=j.id EXCEPT SELECT member_id FROM job_members WHERE job_id=?) '
                'AND NOT EXISTS(SELECT member_id FROM job_members WHERE job_id=? EXCEPT SELECT member_id FROM job_members WHERE job_id=j.id)', (unit['id'],job['role'],_json(revisions),job_id,job_id)).fetchone()[0]
            delays = tuple(_settings(db,'runtime.json').get('reliability',{}).get('claim_retest_seconds',RETRY_SECONDS))
            if not delays or any(type(value) is not int or not 30<=value<=7200 for value in delays) or list(delays)!=sorted(set(delays)) or len(delays)>8:
                raise TransitionError('claim retest delays require ascending bounded seconds')
            repeated = previous is not None or count >= len(delays)
            retry_at = now + delays[min(count,len(delays)-1)]
            db.execute('INSERT INTO worker_claim_attempts VALUES(?,?,?,?,?,?,?,?)',
                (job_id,key,details['reason'],_json(details),_json(revisions),'repeated' if repeated else 'deferred',retry_at,now))
            if repeated:
                failure = {**evidence,'reason':'repeated_worker_claim','claim_key':key,'claim_revisions':revisions}
                self._result(db,command,job,failure,'failure')
                from .recovery import record_failure_facts
                record_failure_facts(db,unit,job,failure,now,store=self.store)
                charge_id = _id(command.request_id,'charge')
                db.execute("INSERT INTO budget_entries(id,attempt_id,kind,reason_code,created_at) VALUES(?,?,'charge','execution_failure',?)", (charge_id,job['attempt_id'],now))
                self._event(db,command,'budget_charged',{'charge_id':charge_id,'reason':'repeated_worker_claim'},job_id=job_id)
                if job['member_id']:
                    db.execute("UPDATE unit_issues SET state='failed' WHERE id=?", (job['member_id'],))
                self._open_gate(db,command,'recovery_pending',{'job_id':job_id,'reason':'repeated_worker_claim'},job_id=job_id)
                if _charges(db,unit)>=3:
                    self._open_gate(db,command,'budget_exhausted',{'limit':3})
                    self._fence(db,command)
                return {'accepted':True,'charged':True,'recovery_required':True,'claim_key':key}
            self._result(db,command,job,evidence,'deferred')
            gates = []
            for pin in db.execute('SELECT member_id FROM job_members WHERE job_id=? ORDER BY member_id', (job_id,)).fetchall():
                prior = db.execute("SELECT * FROM unit_gates WHERE unit_id=? AND member_id=? AND code='environment' AND resolved_at IS NULL", (unit['id'],pin[0])).fetchone()
                if prior:
                    if not eligible_gate_in(db,prior):
                        raise TransitionError('independent environment decision must finish before accepting a worker claim')
                    hydrate_in(db,unit,prior,now)
                    gate_id = prior['id']
                else:
                    gate_id = self._open_gate(db,command,'environment',{**details,'source':'worker_claim','job_id':job_id,'retry_at':retry_at},pin[0])
                db.execute('INSERT INTO worker_claim_obligations(gate_id,job_id,member_id) VALUES(?,?,?)', (gate_id,job_id,pin[0]))
                gates.append(gate_id)
            return {'accepted':True,'charged':False,'gate_ids':gates,'gate_id':gates[0] if len(gates)==1 else None}
        return self._apply(command,'defer_worker_claim',payload,change)

    def recheck_in(self, db, command, unit, gate_ids, *, member_id=None, explicit=False, reason=None):
        resolved, released = [], []
        now = self.clock()
        for gate_id in gate_ids:
            gate = db.execute("SELECT * FROM unit_gates WHERE id=? AND unit_id=? AND code='environment' AND resolved_at IS NULL", (gate_id,unit['id'])).fetchone()
            if gate is None or not eligible_gate_in(db,gate):
                continue
            hydrate_in(db,unit,gate,now)
            obligations = db.execute('SELECT o.*,c.reason,c.revisions_json,c.retry_at FROM worker_claim_obligations o '
                'JOIN worker_claim_attempts c ON c.job_id=o.job_id JOIN unit_issues m ON m.id=o.member_id '
                'WHERE o.gate_id=? AND o.released_at IS NULL AND m.unit_id=? AND m.left_at IS NULL '
                'AND (? IS NULL OR o.member_id=?)', (gate_id,unit['id'],member_id,member_id)).fetchall()
            for obligation in obligations:
                changed = json.loads(obligation['revisions_json']) != revisions_in(db,unit,obligation['job_id'],obligation['reason'])
                release = reason if explicit and reason else 'operator' if explicit else 'relevant_change' if changed else 'timer' if now>=obligation['retry_at'] else None
                if release is None:
                    continue
                db.execute('UPDATE worker_claim_obligations SET released_at=?,release_reason=? WHERE gate_id=? AND job_id=? AND member_id=?',
                    (now,release,gate_id,obligation['job_id'],obligation['member_id']))
                released.append({'gate_id':gate_id,'job_id':obligation['job_id'],'member_id':obligation['member_id'],'reason':release})
            pending = db.execute('SELECT 1 FROM worker_claim_obligations o JOIN unit_issues m ON m.id=o.member_id '
                'WHERE o.gate_id=? AND o.released_at IS NULL AND m.unit_id=? AND m.left_at IS NULL', (gate_id,unit['id'])).fetchone()
            if not pending and obligations:
                self._resolve(db,command,gate_id,{'verified':True,'observed_at':now,'worker_claim_retests':released})
                resolved.append(gate_id)
        if released:
            self._event(db,command,'lifecycle_changed',{'worker_claim_retests':released})
        return {'resolved':resolved,'released':released}

    def recheck(self, command, gate_id):
        # No decision/version churn for ordinary observations before their due date.
        with self.store.read_snapshot() as db:
            gate = db.execute('SELECT * FROM unit_gates WHERE id=? AND unit_id=?', (gate_id,command.unit_id)).fetchone()
            if not gate or not eligible_gate_in(db,gate):
                raise TransitionError('this environment gate is not a worker claim')
            if gate['resolved_at'] is not None:
                return {'gate_id':gate_id,'resolved':True}
            unit = dict(db.execute('SELECT * FROM units WHERE id=?', (command.unit_id,)).fetchone())
            waits = db.execute('SELECT c.* FROM worker_claim_obligations o JOIN worker_claim_attempts c ON c.job_id=o.job_id WHERE o.gate_id=? AND o.released_at IS NULL', (gate_id,)).fetchall()
            if waits and all(self.clock()<row['retry_at'] and json.loads(row['revisions_json'])==revisions_in(db,unit,row['job_id'],row['reason']) for row in waits):
                return {'gate_id':gate_id,'pending':'worker_claim_retest','retry_at':min(row['retry_at'] for row in waits)}
        def change(db,unit):
            self._live(unit)
            result = self.recheck_in(db,command,unit,[gate_id])
            return {'gate_id':gate_id,'resolved':gate_id in result['resolved'],**{'released':result['released']}}
        return self._apply(command,'recheck_worker_claim',{'gate_id':gate_id},change)


def member_claims_in(db, unit_id, member_id, now):
    """Preserve unresolved obligations when approved membership changes units."""
    unit = dict(db.execute('SELECT * FROM units WHERE id=?', (unit_id,)).fetchone())
    for gate in db.execute("SELECT * FROM unit_gates WHERE unit_id=? AND code='environment' AND resolved_at IS NULL AND (member_id IS NULL OR member_id=?)", (unit_id,member_id)).fetchall():
        if eligible_gate_in(db,gate):
            hydrate_in(db,unit,gate,now)
    return [dict(row) for row in db.execute('SELECT o.job_id,c.details_json FROM worker_claim_obligations o '
        'JOIN worker_claim_attempts c ON c.job_id=o.job_id JOIN unit_gates g ON g.id=o.gate_id '
        'WHERE g.unit_id=? AND g.resolved_at IS NULL AND o.member_id=? AND o.released_at IS NULL ORDER BY o.job_id', (unit_id,member_id))]


def inherit_claims_in(lifecycle, db, command, member_id, claims):
    if not claims:
        return
    gate_id = lifecycle._open_gate(db,command,'environment',
        {**json.loads(claims[0]['details_json']),'source':'worker_claim','job_id':claims[0]['job_id']},member_id)
    db.executemany('INSERT INTO worker_claim_obligations(gate_id,job_id,member_id) VALUES(?,?,?)',
        [(gate_id,claim['job_id'],member_id) for claim in claims])
