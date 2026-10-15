"""Typed build result v2; prose explains evidence and never chooses a transition."""
from copy import deepcopy
import json

CLAIMS = ('wait_on_issue', 'needs_decision', 'environment', 'too_large')


def _text(value, limit=4000):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def validate(value, job):
    from .contracts import result_v1
    fields = {'version','attempt','role','outcome','summary','headline','plain','operator_summary',
              'claims','verification','delegation_plan','output_commit','pr_title','pr_body'}
    if (not isinstance(value,dict) or len(json.dumps(value))>65536 or set(value)!=fields or type(value['version']) is not int or value['version']!=2
            or value['role']!='build' or value['attempt']!=job['id'] or value['outcome'] not in {'done','cannot_complete'}
            or not isinstance(value['claims'],list) or len(value['claims'])>20):
        raise ValueError('Build result does not satisfy version 2')
    claims=value['claims'];seen=set()
    for claim in claims:
        if not isinstance(claim,dict) or claim.get('type') not in CLAIMS:
            raise ValueError('Unknown build claim')
        kind=claim['type']
        if kind=='wait_on_issue':
            if (set(claim)!={'type','repository_id','issue_number','reason'}
                    or type(claim['repository_id']) is not int or claim['repository_id']<=0
                    or type(claim['issue_number']) is not int or claim['issue_number']<=0 or not _text(claim['reason'])):
                raise ValueError('Dependency claim needs a typed issue identity')
            identity=(kind,claim['repository_id'],claim['issue_number'])
            assigned={m['issue_number'] for m in job.get('members',[])}|{job.get('issue_number')}
            if claim['repository_id']==job['repo_id'] and claim['issue_number'] in assigned:
                raise ValueError('An assignment cannot wait on its own members')
        elif kind=='needs_decision':
            if (set(claim)!={'type','question','options','recommendation','evidence'}
                    or any(not _text(claim[k]) for k in ('question','recommendation','evidence'))
                    or not isinstance(claim['options'],list) or not 2<=len(claim['options'])<=6
                    or any(not _text(o,1000) for o in claim['options']) or len(set(claim['options']))!=len(claim['options'])):
                raise ValueError('Decision claim needs distinct options and evidence')
            identity=(kind,)
        elif kind=='environment':
            from .environments import services
            if (set(claim)!={'type','environment_id','services','reason','evidence'}
                    or not _text(claim['environment_id'],80) or claim['reason'] not in {'toolchain','sandbox','dependency_access','resources','ci_evidence','services'} or not _text(claim['evidence'])):
                raise ValueError('Environment claim needs a tier and evidence')
            services(claim['services'])
            if (claim['reason']=='services')!=bool(claim['services']):
                raise ValueError('Only service claims name missing services')
            configured=(job.get('runner_config') or {}).get('environment_tiers',{})
            if claim['environment_id'] not in configured or claim['environment_id']!='full' and claim['services']:
                raise ValueError('Environment claim must name a configured compatible tier')
            identity=(kind,)
        else:
            if set(claim)!={'type','reason'} or not _text(claim['reason']):
                raise ValueError('Scope claim needs an explanation')
            identity=(kind,)
        if identity in seen:
            raise ValueError('Duplicate build claim')
        seen.add(identity)
    if value['outcome']=='done' and claims:
        raise ValueError('Completed work cannot retain blocking claims')
    if value['outcome']=='cannot_complete' and any(value[k] is not None for k in
            ('output_commit','pr_title','pr_body','verification','delegation_plan')):
        raise ValueError('Incomplete work cannot supply completion artifacts')
    # Reuse the exact publication, delegation and verification validation. This
    # is a compatibility projection; the persisted result remains version 2.
    if not job.get('source_input') and any(value[k] is not None for k in ('pr_title','pr_body')):
        raise ValueError('This assignment cannot publish pull request content')
    if not (job.get('source_input') or job.get('builder_stage') or job.get('task')) and any(value[k] is not None for k in ('delegation_plan','output_commit')):
        raise ValueError('This assignment cannot supply managed output artifacts')
    projected=legacy(value,job)
    checked=result_v1(projected,job)
    result=deepcopy(value)
    for key in ('headline','plain','operator_summary'):
        result[key]=checked[key]
    return result


def legacy(value, job):
    """Adapter for old execution helpers, never inferred from summary text."""
    result={k:deepcopy(v) for k,v in value.items() if k!='claims'}
    result['version']=1
    result['outcome']=('delegate' if job.get('builder_stage')=='planning' else
                       'needs_human_verification' if value.get('verification') else 'pass') if value['outcome']=='done' else 'unable'
    if not (job.get('source_input') or job.get('builder_stage') or job.get('task')):
        result.pop('delegation_plan');result.pop('output_commit')
    if not job.get('source_input'):
        result.pop('pr_title');result.pop('pr_body')
    return result


def schema(job):
    from .harness import plan_schema
    def obj(properties):
        return {'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}
    text=lambda limit=4000:{'type':'string','minLength':1,'maxLength':limit}
    tagged=lambda kind,props:obj({'type':{'type':'string','enum':[kind]},**props})
    integer={'type':'integer','minimum':1}
    environments=sorted((job.get('runner_config') or {}).get('environment_tiers',{}))
    variants=[tagged('wait_on_issue',{'repository_id':integer,'issue_number':integer,'reason':text()}),
        tagged('needs_decision',{'question':text(),'options':{'type':'array','minItems':2,'maxItems':6,'items':text(1000)},
            'recommendation':text(),'evidence':text()}),tagged('too_large',{'reason':text()})]
    if environments:
        variants.append(tagged('environment',{'environment_id':{'type':'string','enum':environments},
            'services':{'type':'array','maxItems':3,'items':{'type':'string','enum':['mongodb','postgres','redis']}},
            'reason':{'type':'string','enum':['toolchain','sandbox','dependency_access','resources','ci_evidence','services']},'evidence':text()}))
    nullable=lambda spec:{'anyOf':[{'type':'null'},spec]}
    return obj({'version':{'type':'integer','enum':[2]},'attempt':{'type':'string','enum':[job['id']]},
        'role':{'type':'string','enum':['build']},'outcome':{'type':'string','enum':['done','cannot_complete']},
        'summary':text(16000),'headline':text(90),'plain':text(360),'operator_summary':text(300),
        'claims':{'type':'array','maxItems':20,'items':{'anyOf':variants}},
        'verification':nullable(obj({'what':text(),'why':text(),'checks':{'type':'array','maxItems':20,'items':text(200)}})),
        'delegation_plan':nullable(plan_schema()),'output_commit':{'type':['string','null']},
        'pr_title':nullable(text(256)),'pr_body':nullable(text(12000))})
