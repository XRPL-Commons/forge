"""Preserve controller inputs without turning a legacy issue blob into runtime state."""
from .normalize import batches, insert, finding, artifact
from .supporting import json_text, unknown_fields
from .snapshot import decode
from .supporting_parity import require

CONTEXT_FIELDS = [('summary_json','last_summary'),('retry_plan_json','retry_plan'),
                  ('decision_request_json','decision_request'),('verification_request_json','verification_request')]
ENVIRONMENT_FIELDS = [('choice_json','environment_choice'),('override_json','environment_override'),
                      ('retry_json','environment_retry'),('memory_retry_json','memory_retry')]
KNOWN = ('key','repo_id','repo','number','title','state','reason','requirements','candidate','active','failures','budget_start','created_at',
         'blocked_dependencies','budget_interruptions','checks_settled','closed_as','decision_version','declared_dependencies',
         'dependencies_checked_at','dependency_body','dependency_evidence','dependency_reason','environment_choice','environment_hold',
         'environment_label','environment_override','environment_retry','memory_retry','evidence','github_observation','label_hold',
         'last_observed','last_summary','merge_gate','operator_hold','operator_sync','operator_request','ownership_wait','prerequisite_hold',
         'retry_after','retry_plan','settled_observed','decision_request','verification_request')


def normalize(importer, ledger):
    def unit(db,pair):
        key,value=pair
        identifier='legacy:unit:'+key
        insert(db,'unit_context',unit_id=identifier,reason=value.get('reason'),checks_settled=value.get('checks_settled'),
               last_observed_at=value.get('last_observed'),settled_observed=value.get('settled_observed'),
               observation_interruptions=value.get('budget_interruptions',0),
               **{column:json_text(value.get(field)) for column,field in CONTEXT_FIELDS})
        insert(db,'unit_environment_observations',unit_id=identifier,label=value.get('environment_label'),
               **{column:json_text(value.get(field)) for column,field in ENVIRONMENT_FIELDS})
        insert(db,'dependency_observations',unit_id=identifier,requirements_body=value.get('dependency_body'),
               evidence=value.get('dependency_evidence'),reason=value.get('dependency_reason'),checked_at=value.get('dependencies_checked_at'))
        for kind,field in [('blocked','blocked_dependencies'),('declared','declared_dependencies')]:
            for index,member in enumerate(value.get(field,[])):
                insert(db,'dependency_observation_members',unit_id=identifier,kind=kind,position=index,value_json=json_text(member))
        for index,failure in enumerate(value['failures']):
            insert(db,'attempt_failures',attempt_id=failure['attempt'],unit_id=identifier,position=index,role=failure.get('role'),
                   cause=failure.get('cause'),investigation=failure.get('investigation'),retry_stage=failure.get('retry_state'),
                   evidence_kind='failure_evidence',findings_json=json_text(failure.get('findings')),
                   summary_json=json_text(failure.get('summary')),at=failure['at'])
            artifact(db,failure['attempt'],'failure_evidence',failure.get('evidence'),failure['at'])
            unknown_fields(db,importer,key,failure,('attempt','role','cause','investigation','retry_state','evidence','findings','summary','at','charged'))
        unknown_fields(db,importer,key,value,KNOWN)
        if value.get('operator_request'):
            # Its original request ID and expected revision must be reconciled
            # with the external label effect before dispatch may resume.
            finding(db,importer,key,'operator_effect_requires_reconciliation')
    batches(importer,'normalized:delivery_context',sorted(ledger['issues'].items()),unit)
    with importer.store.write() as db:
        unknown_fields(db,importer,'ledger',ledger,('version','issues','jobs','slots','events','provider_cooldowns','provider_cursor','memory_profiles','discovery_observed'))


def check(importer):
    from ..artifacts import Artifacts
    ledger=importer.source.json('state/factory/factory-ledger.json')
    counts={'unit_contexts':0,'attempt_failures':0,'dependency_members':0}
    with importer.store.read_snapshot() as db:
        for key,value in ledger['issues'].items():
            identifier='legacy:unit:'+key
            row=db.execute('SELECT * FROM unit_context WHERE unit_id=?',(identifier,)).fetchone()
            require(row is not None,'unit_context')
            for column,field in CONTEXT_FIELDS:
                require((decode(row[column]) if row[column] is not None else None)==value.get(field),'unit_context')
            for column,field in [('reason','reason'),('checks_settled','checks_settled'),('last_observed_at','last_observed'),('settled_observed','settled_observed')]:
                require(row[column]==value.get(field),'unit_context')
            require(row['observation_interruptions']==value.get('budget_interruptions',0),'unit_context')
            row=db.execute('SELECT * FROM unit_environment_observations WHERE unit_id=?',(identifier,)).fetchone()
            require(row is not None and row['label']==value.get('environment_label'),'unit_environment')
            for column,field in ENVIRONMENT_FIELDS:
                require((decode(row[column]) if row[column] is not None else None)==value.get(field),'unit_environment')
            row=db.execute('SELECT * FROM dependency_observations WHERE unit_id=?',(identifier,)).fetchone()
            for column,field in [('requirements_body','dependency_body'),('evidence','dependency_evidence'),('reason','dependency_reason'),('checked_at','dependencies_checked_at')]:
                require(row is not None and row[column]==value.get(field),'dependency_observation')
            for kind,field in [('blocked','blocked_dependencies'),('declared','declared_dependencies')]:
                rows=db.execute('SELECT value_json FROM dependency_observation_members WHERE unit_id=? AND kind=? ORDER BY position',(identifier,kind)).fetchall()
                require([decode(r[0]) for r in rows]==value.get(field,[]),'dependency_members')
                counts['dependency_members']+=len(rows)
            for failure in value['failures']:
                row=db.execute('SELECT * FROM attempt_failures WHERE attempt_id=?',(failure['attempt'],)).fetchone()
                require(row is not None,'attempt_failure')
                for column,field in [('role','role'),('cause','cause'),('investigation','investigation'),('retry_stage','retry_state'),('at','at')]:
                    require(row[column]==failure.get(field),'attempt_failure')
                for field in ('findings','summary'):
                    require((decode(row[field+'_json']) if row[field+'_json'] is not None else None)==failure.get(field),'attempt_failure')
                chunks=[Artifacts(importer.store).read(failure['attempt'],row['evidence_kind'],r[0]).content for r in
                        db.execute('SELECT sequence FROM job_artifacts WHERE job_id=? AND kind=? ORDER BY sequence',(failure['attempt'],row['evidence_kind']))]
                require(decode(b''.join(chunks))==failure.get('evidence'),'failure_evidence')
                counts['attempt_failures']+=1
            counts['unit_contexts']+=1
    return counts
