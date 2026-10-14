"""Explicit historical records for upgrade tests; never use current lifecycle writers.

These small fixtures exercise historical storage contracts without asking today's
commands to write a schema they no longer support. No runtime fallback is needed.
"""
import json

from forge.store.transitions import manifest_hash


def member(db, *, unit='u', issue='1:1', number=1, identifier='member', requirements=None):
    requirements = {'acceptance': ['works']} if requirements is None else requirements
    revision = 'revision-' + identifier
    db.execute("INSERT OR IGNORE INTO repositories(id,full_name) VALUES(1,'owner/repo')")
    db.execute("INSERT OR IGNORE INTO units(id,repository_id,base_branch,state,created_at,updated_at) VALUES(?,1,'main','queued',90,100)", (unit,))
    db.execute("INSERT INTO issues(id,repository_id,github_number,github_state,current_revision_id,observed_at) VALUES(?,1,?,'open',?,90)", (issue, number, revision))
    db.execute("INSERT INTO issue_revisions(id,issue_id,title,body,content_hash,requirements_hash,requirements_json,observed_at) VALUES(?,?,'Issue','Body','body',?,?,90)", (revision, issue, manifest_hash(requirements), json.dumps(requirements)))
    db.execute("INSERT INTO unit_issues(id,unit_id,issue_id,repository_id,base_branch,requirements_revision_id,state,joined_at) VALUES(?,?,?,1,'main',?,'pending',90)", (identifier, unit, issue, revision))
    return revision


def gate(db, code, *, candidate=None, job=None):
    details = {'reason': 'observed', 'retry_at': 200}
    if job:
        details['job_id'] = job
    db.execute("INSERT INTO unit_gates(id,unit_id,candidate_id,subject_key,code,details_json,created_at) VALUES('gate','u',?,?,?,?,100)",
               (candidate, 'candidate:' + candidate if candidate else 'unit', code, json.dumps(details)))
    db.execute("UPDATE units SET primary_gate_id='gate' WHERE id='u'")


def status_case(store, name):
    with store.write() as db:
        revision = member(db)
        if name in {'working', 'cleanup_pending', 'result_pending'}:
            inputs = json.dumps(dict(requirements_revision_id=revision, base_oid='base', policy_hash='policy'))
            db.execute("UPDATE units SET state='building' WHERE id='u'")
            db.execute("INSERT INTO attempts VALUES('attempt','u','member',1,1,?,1,100)", (inputs,))
            cleanup = name == 'result_pending'
            fenced = name != 'working'
            db.execute("INSERT INTO jobs(id,unit_id,attempt_id,member_id,role,phase,fenced,cleanup,created_at) VALUES('job','u','attempt','member','build',?,?,?,100)",
                       ('cleaned' if cleanup else 'reserved', int(fenced), int(cleanup)))
            if name != 'working':
                gate(db, name, job='job')
        elif name in {'merged', 'reopened', 'approval_required', 'verification_ci', 'verification_human'}:
            db.execute("INSERT INTO candidates VALUES('candidate','u',10,'branch','head','base','policy','requirements',1,'inputs',100)")
            db.execute("UPDATE units SET current_candidate_id='candidate' WHERE id='u'")
            if name in {'merged', 'reopened'}:
                db.execute("UPDATE unit_issues SET state='done',completion_issue_version=1,left_at=100,exit_reason='merged' WHERE id='member'")
                db.execute("UPDATE units SET state='reviewing' WHERE id='u'")
                db.execute("UPDATE units SET state='merged',merge_sha='merge' WHERE id='u'")
                if name == 'reopened':
                    db.execute("UPDATE issues SET state_reason='reopened',reopened_version=2,version=2 WHERE id='1:1'")
            else:
                gate(db, name, candidate='candidate')
        elif name in {'completed_elsewhere', 'dropped'}:
            reason = 'completed' if name == 'completed_elsewhere' else 'not_planned'
            db.execute("UPDATE issues SET github_state='closed',state_reason=? WHERE id='1:1'", (reason,))
            db.execute("UPDATE unit_issues SET state=?,left_at=100,exit_reason='closed' WHERE id='member'", ('done' if name == 'completed_elsewhere' else 'pending',))
            db.execute("UPDATE units SET state='closed',close_code=? WHERE id='u'", ('github_completed' if name == 'completed_elsewhere' else 'github_not_planned',))
        elif name != 'queued':
            if name == 'independent_hold':
                db.execute("INSERT INTO unit_gates(id,unit_id,subject_key,code,details_json,created_at,resolved_at,resolution) VALUES('previous-pause','u','unit','operator_pause','{}',90,99,'operator')")
            gate(db, 'environment' if name == 'independent_hold' else name)


def accepted_dependency(store):
    """A v1 grouped plan with one exact accepted output and one waiting member."""
    with store.write() as db:
        pins = []
        for number in range(2):
            identifier = 'member-' + str(number)
            revision = member(db, unit='batch', issue='issue-' + str(number), number=number+1, identifier=identifier)
            pins.append(dict(member_id=identifier, requirements_revision_id=revision))
        db.execute("UPDATE units SET state='building' WHERE id='batch'")
        db.execute("INSERT INTO unit_publication_targets VALUES('batch','{\"kind\":\"new\"}')")
        db.execute("INSERT INTO unit_builders VALUES('builder','batch','operator',100)")
        db.execute("INSERT INTO member_dependencies VALUES('batch','member-1','member-0',1)")
        parent = dict(members=pins, base_oid='base', policy_hash='policy', accepted_outputs=[])
        db.execute("INSERT INTO attempts VALUES('parent','batch',NULL,1,1,?,1,100)", (json.dumps(parent),))
        db.execute("INSERT INTO jobs(id,unit_id,attempt_id,role,phase,outcome,fenced,cleanup,created_at) VALUES('parent','batch','parent','build','cleaned','planned',1,1,100)")
        db.execute("INSERT INTO builder_assignments VALUES('parent','builder','planning')")
        plan = {'children': [dict(key=str(n), member_id=pin['member_id'], scope='Implement member', dependencies=[] if n == 0 else ['member-0']) for n, pin in enumerate(pins)]}
        db.execute("INSERT INTO delegation_plans VALUES('plan','builder','parent',?,?,100)", (manifest_hash(parent), json.dumps(plan)))
        inputs = dict(requirements_revision_id=pins[0]['requirements_revision_id'], base_oid='base', policy_hash='policy', prerequisite_outputs=[], delegation_plan_id='plan', delegation_key='0', scope='Implement member')
        db.execute("INSERT INTO attempts VALUES('child','batch','member-0',1,1,?,1,100)", (json.dumps(inputs),))
        db.execute("INSERT INTO jobs(id,unit_id,attempt_id,member_id,role,phase,outcome,fenced,cleanup,created_at) VALUES('child','batch','child','member-0','build','cleaned','success',1,1,100)")
        db.execute("INSERT INTO job_delegations VALUES('child','plan','0','tree-0')")
        db.execute("INSERT INTO part_outputs VALUES('output','batch','member-0','child',?,1,?,'base',?,100)", (pins[0]['requirements_revision_id'], 'a'*40, manifest_hash(inputs)))


def reservation(store, *, config, policy, base_oid):
    """A schema-028 reserved worker, including the immutable scheduling event."""
    with store.write() as db:
        for unit, number in (('u', 1), ('v', 2)):
            member(db, unit=unit, issue='issue-'+unit, number=number,
                   identifier='member-'+unit, requirements='github-requirements')
        inputs = dict(requirements_revision_id='revision-member-u', base_oid=base_oid,
                      policy_hash=manifest_hash(policy))
        db.execute("UPDATE units SET state='building' WHERE id='u'")
        db.execute("INSERT INTO attempts VALUES('attempt','u','member-u',1,1,?,1,100)", (json.dumps(inputs),))
        db.execute("INSERT INTO jobs(id,unit_id,attempt_id,member_id,role,phase,created_at) VALUES('job','u','attempt','member-u','build','reserved',100)")
        db.execute("INSERT INTO job_leases VALUES('build-1','job','fence',100)")
        db.execute("INSERT INTO job_launches(job_id,slot,execution_fence,branch,publisher,provider,runner_config_json,policy_json,context_json,deadline) VALUES('job','build-1','fence','forge/issue-1','build[bot]','codex',?,?,'{}',2800)", (json.dumps(config),json.dumps(policy)))
        db.execute("INSERT INTO job_execution_state(job_id) VALUES('job')")
        db.execute("INSERT INTO events(transaction_id,unit_id,job_id,kind,at,actor_id,payload_json) VALUES('reserve','u','job','attempt_reserved',100,'operator','{}')")
    return 'job'


def investigation_035(store):
    """A completed investigation under schema 035, before recovery table rebuilds."""
    findings = dict(summary='Investigated failure', pattern='missing dependency',
                    evidence=['preserved test output'], uncertainty='', alternative=None, incomplete=False)
    with store.write() as db:
        revision = member(db)
        inputs = json.dumps(dict(requirements_revision_id=revision, base_oid='base', policy_hash='policy'))
        for identifier, role, outcome in (('failed', 'build', 'failure'), ('recovery', 'recovery', 'investigated')):
            db.execute("INSERT INTO attempts VALUES(?,'u','member',1,1,?,1,100)", (identifier, inputs))
            db.execute("INSERT INTO jobs(id,unit_id,attempt_id,member_id,role,phase,outcome,fenced,cleanup,created_at) "
                       "VALUES(?,'u',?,'member',?,'cleaned',?,1,1,100)", (identifier, identifier, role, outcome))
        db.execute("INSERT INTO attempt_failures(attempt_id,unit_id,position,role,cause,investigation,retry_stage,evidence_kind,findings_json,at,recovery_required) "
                   "VALUES('failed','u',0,'build','failed tests','complete','build','failure_evidence',?,100,1)", (json.dumps(findings),))
        db.execute("INSERT INTO recovery_assignments VALUES('recovery','failed','failed',100)")
        db.execute("INSERT INTO recovery_findings VALUES('recovery',0,1,?,100)", (json.dumps(findings),))
        db.execute("INSERT INTO budget_entries(id,attempt_id,kind,reason_code,created_at) "
                   "VALUES('charge','failed','charge','execution_failure',100)")
