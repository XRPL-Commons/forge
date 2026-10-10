"""Compare normalized durable facts with the frozen source, independently of import cursors."""
from ..artifacts import Artifacts
from ..transitions import project_unit
from ...factory.ledger import Ledger
from .snapshot import MigrationError, decode, encode, digest


def check(importer):
    source=importer.source.ledger()
    counters={'units':0,'jobs':0,'events':0,'budget_histories':0,'candidates':0,'artifact_chunks':0}
    with importer.store.read_snapshot() as db:
        for key,item in source['issues'].items():
            view=project_unit(db,'legacy:unit:'+key)
            if (view['created_at']!=item['created_at'] or view['charged_failures']!=Ledger.failure_count(item)
                    or view['version']!=max(1,item.get('decision_version',0))):
                raise MigrationError('unit_parity_mismatch')
            counters['units']+=1
            member=db.execute('SELECT r.requirements_hash FROM unit_issues m JOIN issue_revisions r ON r.id=m.requirements_revision_id WHERE m.unit_id=?',(view['id'],)).fetchone()
            if item.get('requirements') and member[0]!=item['requirements']:
                raise MigrationError('requirements_parity_mismatch')
            for failure in item['failures']:
                charge=db.execute("SELECT count(*) FROM budget_entries WHERE attempt_id=? AND kind='charge'",(failure['attempt'],)).fetchone()[0]
                correction=db.execute("SELECT count(*) FROM budget_entries WHERE attempt_id=? AND kind='correction'",(failure['attempt'],)).fetchone()[0]
                if charge!=1 or correction!=int(not failure['charged']):
                    raise MigrationError('budget_history_parity_mismatch')
            counters['budget_histories']+=1
            original=item.get('candidate')
            if original and view['current_candidate_id']:
                candidate=db.execute('SELECT * FROM candidates WHERE id=?',(view['current_candidate_id'],)).fetchone()
                for new,old in [('pull_number','pr'),('head_oid','head'),('base_oid','base'),('policy_hash','policy'),('requirements_hash','requirements')]:
                    if candidate[new]!=original[old]:
                        raise MigrationError('candidate_parity_mismatch')
                counters['candidates']+=1
        for identifier,job in source['jobs'].items():
            row=db.execute('SELECT * FROM jobs WHERE id=?',(identifier,)).fetchone()
            if row is None or row['outcome']!=job['outcome'] or row['created_at']!=job['created_at'] or bool(row['cleanup'])!=job['cleanup'] or row['role']!=job['role']:
                raise MigrationError('job_parity_mismatch')
            counters['jobs']+=1
        for event in source['events']:
            row=db.execute('SELECT * FROM events WHERE sequence=?',(event['sequence'],)).fetchone()
            if (row is None or row['at']!=event['at'] or row['issue_id']!=event['issue']
                    or decode(row['payload_json'])['digest']!=digest(encode(event)) or decode(row['payload_json'])['legacy']!=event):
                raise MigrationError('event_parity_mismatch')
            counters['events']+=1
        for table,count in [('units',len(source['issues'])),('jobs',len(source['jobs'])),('events',len(source['events']))]:
            if db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]!=count:
                raise MigrationError('normalized_row_count_mismatch')
        artifacts=[tuple(row) for row in db.execute('SELECT job_id,kind,sequence FROM job_artifacts ORDER BY job_id,kind,sequence')]
    reader=Artifacts(importer.store)
    # Validate every chunk's codec, bounded length and digest, then compare each
    # semantic result/output to the legacy fact. No bytes go into the report.
    grouped={}
    for job,kind,sequence in artifacts:
        grouped.setdefault((job,kind),[]).append(reader.read(job,kind,sequence).content)
        counters['artifact_chunks']+=1
    for identifier,job in source['jobs'].items():
        capture=job.get('capture') or {}
        expected={'result':{k:v for k,v in capture.items() if k!='logs'}}
        if 'logs' in capture:expected['logs']=capture['logs']
        if job.get('observation'):expected['live_tail']=job['observation']
        if job.get('ci_evidence'):expected['ci_transcript']=job['ci_evidence']
        for kind,value in expected.items():
            if decode(b''.join(grouped.get((identifier,kind),[])))!=value:
                raise MigrationError('artifact_parity_mismatch')
    return counters
