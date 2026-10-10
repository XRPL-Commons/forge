"""Expand immutable worker wait receipts for operator presentation.

Gate authority keeps compact references and canonical dependency/service unions;
individual prose stays in the original decisions, outside the gate byte limit.
"""
import json
from .models import TransitionError


def details_in(db,gate):
    value=json.loads(gate['details_json'])
    references=value.get('worker_waits',[]) if value.get('source')=='worker_waits' else []
    if not references: return value
    waits=[{'job_id':value.get('job_id'),'details':value}]
    for reference in references:
        row=db.execute('SELECT unit_id,command,status,payload_json FROM decisions WHERE request_id=?',(reference['request_id'],)).fetchone()
        if row is None or row['unit_id']!=gate['unit_id'] or row['command']!='defer_job' or row['status']!='applied':
            raise TransitionError('worker wait receipt does not match its gate')
        payload=json.loads(row['payload_json'])
        if payload['job_id']!=reference['job_id'] or payload['code']!=gate['code']:
            raise TransitionError('worker wait receipt changed assignment or kind')
        waits.append({'job_id':reference['job_id'],'details':payload['details']})
    for wait in waits:
        row=db.execute('SELECT t.task_key FROM job_tasks j JOIN delivery_tasks t ON t.id=j.task_id WHERE j.job_id=?',(wait['job_id'],)).fetchone()
        wait['label']='Task '+row[0] if row else 'Existing decision'
    result={**value,'waits':[{'job_id':wait['job_id'],'label':wait['label'],'details':
        {key:entry for key,entry in wait['details'].items() if key not in {'worker_waits','previous_gate_id'}}} for wait in waits]}
    fields=('question','recommendation','evidence') if gate['code']=='decision_required' else ('summary','operator_summary','evidence')
    for field in fields:
        parts=[wait['label']+': '+wait['details'][field] for wait in waits if wait['details'].get(field)]
        if parts: result[field]='\n\n'.join(parts)
    if gate['code']=='decision_required':
        result['options']=[wait['label']+': '+option for wait in waits for option in wait['details'].get('options',[])]
    return result
