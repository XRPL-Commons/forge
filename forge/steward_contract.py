"""The steward proposes bounded typed choices; only the core grants authority."""
from copy import deepcopy
import hashlib
import json
import math

from .store.models import TransitionError

MODES = frozenset({'recovery', 'stall', 'brief', 'triage', 'conversation'})
ACTIONS = frozenset({'retry', 'wait_on', 'rebuild', 'split', 'drop', 'fold', 'escalate_forge', 'question'})
REVERSIBLE = frozenset({'retry', 'wait_on', 'rebuild'})
MAX_CYCLES = 3


def encoded(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    except (TypeError, ValueError):
        raise TransitionError('Steward data must be finite JSON') from None


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def require(value, message):
    if not value:
        raise TransitionError(message)


def fields(value, required, optional=()):
    require(isinstance(value, dict) and set(required) <= set(value) <= set(required) | set(optional),
            'Unknown or missing steward fields')


def text(value, limit=2000):
    require(isinstance(value, str) and bool(value.strip()) and len(value.encode()) <= limit,
            'Steward text is empty or too large')


def positive(value):
    require(type(value) is int and value > 0, 'Steward identity must be a positive integer')


def reference(value):
    fields(value, ('job_id', 'kind', 'sequence', 'digest'))
    text(value['job_id'], 256); text(value['kind'], 100)
    require(type(value['sequence']) is int and value['sequence'] >= 0, 'Invalid artifact sequence')
    require(isinstance(value['digest'], str) and len(value['digest']) == 64
            and all(c in '0123456789abcdef' for c in value['digest']), 'Invalid artifact digest')


def parameters(action, value):
    if action == 'retry':
        fields(value, (), ('provider_id', 'timeout_seconds', 'environment_id', 'resume'))
        require(bool(value), 'Retry requires a concrete adjustment')
        for key in ('provider_id', 'environment_id'):
            if key in value: text(value[key], 256)
        if 'timeout_seconds' in value:
            positive(value['timeout_seconds'])
        if 'resume' in value:
            fields(value['resume'], ('job_id', 'head_oid'))
            text(value['resume']['job_id'], 256); text(value['resume']['head_oid'], 64)
    elif action in {'wait_on', 'fold'}:
        fields(value, ('repository_id', 'issue_number') + (('intent_note',) if action == 'fold' else ()))
        positive(value['repository_id']); positive(value['issue_number'])
        if action == 'fold': text(value['intent_note'], 8192)
    elif action == 'rebuild':
        fields(value, ('base_oid',)); text(value['base_oid'], 64)
    elif action == 'split':
        fields(value, ('children', 'intent_note')); text(value['intent_note'], 8192)
        require(isinstance(value['children'], list) and 2 <= len(value['children']) <= 8, 'Split needs two to eight drafts')
        for child in value['children']:
            fields(child, ('title', 'body', 'acceptance_criteria'))
            text(child['title'], 256); text(child['body'], 8192)
            require(isinstance(child['acceptance_criteria'], list) and 1 <= len(child['acceptance_criteria']) <= 20,
                    'Split draft needs bounded acceptance criteria')
            for criterion in child['acceptance_criteria']: text(criterion)
    elif action == 'drop':
        fields(value, ('intent_note',)); text(value['intent_note'], 8192)
    elif action == 'escalate_forge':
        fields(value, ('signature',)); text(value['signature'], 256)
    elif action == 'question':
        fields(value, ('text',)); text(value['text'])


def decision(value, dossier, *, accepted=None):
    """One output in all modes. Brief may enrich only the accepted output's brief."""
    fields(value, ('version', 'dossier_id', 'dossier_digest', 'choices',
                   'recommended_choice_id', 'confidence', 'brief'), ('metadata',))
    require(type(value['version']) is int and value['version'] == 1, 'Unsupported steward decision')
    require(value['dossier_id'] == dossier['id'] and value['dossier_digest'] == dossier['digest'],
            'Steward decision belongs to another dossier')
    require(len(encoded(value)) <= 65536, 'Steward decision exceeds its size bound')
    choices = value['choices']
    require(isinstance(choices, list) and 1 <= len(choices) <= 4, 'Steward needs one to four ranked choices')
    identities = set()
    for choice in choices:
        fields(choice, ('id', 'action', 'parameters', 'reason', 'judgement'))
        text(choice['id'], 128); text(choice['reason'])
        require(choice['id'] not in identities, 'Duplicate steward choice')
        identities.add(choice['id'])
        require(choice['action'] in dossier['allowed_moves'], 'Steward move is not offered')
        require(choice['judgement'] in {'obvious', 'judgement'}, 'Invalid steward judgement')
        if dossier['mode'] in {'triage','conversation'}:
            from .steward_subjects import validate_choice
            validate_choice(choice,dossier)
            continue
        require(choice['action'] in ACTIONS,'Unsupported unit move')
        parameters(choice['action'], choice['parameters'])
        if dossier['mode'] != 'brief':
            action, params = choice['action'], choice['parameters']
            capabilities = dossier['facts']['capabilities']
            observed = dossier['facts']['observations']
            if action == 'retry':
                for key, offered in (('provider_id','provider_ids'),('environment_id','environment_ids')):
                    require(key not in params or params[key] in capabilities[offered], 'Retry adjustment is not configured')
                if 'timeout_seconds' in params:
                    bounds = capabilities['timeout_bounds']
                    require(bounds['min'] <= params['timeout_seconds'] <= bounds['max'], 'Retry time exceeds configured bounds')
                if 'resume' in params:
                    require(params['resume']['job_id'] == dossier['trigger'].get('failed_job_id')
                            and dossier['authority']['candidate'] is not None
                            and params['resume']['head_oid'] == dossier['authority']['candidate']['head_oid'],
                            'Resume must identify the exact failed assignment and retained candidate')
            if action == 'escalate_forge':
                require(params['signature'] in dossier['facts']['forge_problem_signatures'],
                        'Forge escalation requires a trusted retained fault signature')
            if action == 'rebuild':
                require(observed['base_oid'] is not None and params['base_oid']==observed['base_oid'],
                        'Rebuild requires the observed current base')
            if action in {'wait_on','fold'}:
                require(any(issue['repository_id']==params['repository_id'] and issue['issue_number']==params['issue_number']
                            and issue['state']=='open' for issue in observed['issues']), 'Target issue was not observed open')

    require(value['recommended_choice_id'] in identities, 'Steward must recommend exactly one choice')
    confidence = value['confidence']
    require(type(confidence) in (int, float) and math.isfinite(confidence) and 0 <= confidence <= 1,
            'Invalid steward confidence')
    brief = value['brief']
    if brief is not None:
        fields(brief, ('choice_id', 'instructions', 'evidence_refs'), ('environment',)); text(brief['instructions'], 8192)
        require(brief['choice_id'] in identities, 'Brief must identify one offered choice')
        require(isinstance(brief['evidence_refs'], list) and len(brief['evidence_refs']) <= 20, 'Too many brief references')
        allowed = {encoded(ref) for ref in dossier['facts']['artifact_refs']}
        for ref in brief['evidence_refs']:
            reference(ref)
            require(encoded(ref) in allowed, 'Brief evidence was not in the dossier')
    if brief is not None and brief.get('environment') is not None:
        environment=brief['environment'];fields(environment,('environment_id','services'))
        require(environment['environment_id'] in dossier['facts']['capabilities']['environment_ids'],'Brief environment is not configured')
        require(isinstance(environment['services'],list) and len(environment['services'])<=20
                and all(isinstance(v,str) and v for v in environment['services']), 'Brief services must be typed identities')
    if 'metadata' in value:
        from .steward_subjects import validate_metadata
        validate_metadata(value['metadata'])
    if dossier['mode'] == 'brief':
        require(accepted is not None and brief is not None, 'Brief requires an accepted decision')
        require(brief['choice_id'] == dossier['trigger']['choice_id'], 'Brief belongs to another selected choice')
        for key in ('choices', 'recommended_choice_id', 'confidence'):
            require(value[key] == accepted[key], 'Brief cannot change an accepted decision')
    return deepcopy(value)


def decision_schema(dossier, accepted=None):
    """Standalone strict JSON schema pinned in the immutable worker assignment.

    Host validation above remains authoritative. Every object declares all its
    fields required; retry alternatives represent absent adjustments explicitly
    through a union of shapes, so strict providers need no nullable coercion.
    """
    from itertools import combinations

    def obj(properties):
        return {'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}

    def string(limit=2000):return {'type':'string','minLength':1,'maxLength':limit}
    def enum(values):return {'type':'string','enum':list(values)}
    def array(items,low=0,high=20):return {'type':'array','items':items,'minItems':low,'maxItems':high}

    if dossier['mode'] in {'triage','conversation'}:
        from .steward_subjects import choice_schemas, metadata_schema
        choices=array({'anyOf':[obj({'id':string(128),'action':action,'parameters':params,
            'reason':string(),'judgement':enum(['obvious','judgement'])}) for action,params in choice_schemas(dossier)]},1,4)
        brief=obj({'choice_id':string(128),'instructions':string(8192),'evidence_refs':array({'type':'null'},0,0),
                   'environment':{'anyOf':[{'type':'null'},obj({'environment_id':enum(dossier['facts']['capabilities']['environment_ids']),
                       'services':array(string(100))})]}})
        if not dossier['facts']['capabilities']['environment_ids']:brief['properties']['environment']={'type':'null'}
        properties={'version':{'type':'integer','enum':[1]},'dossier_id':enum([dossier['id']]),
                    'dossier_digest':enum([dossier['digest']]),'choices':choices,'recommended_choice_id':string(128),
                    'confidence':{'type':'number','minimum':0,'maximum':1},'brief':{'anyOf':[{'type':'null'},brief]}}
        if dossier['mode']=='conversation':properties['metadata']=metadata_schema()
        return obj(properties)

    capabilities=dossier['facts']['capabilities']
    adjustments={}
    for key,offered in (('provider_id','provider_ids'),('environment_id','environment_ids')):
        if capabilities[offered]:adjustments[key]=enum(capabilities[offered])
    adjustments['timeout_seconds']={'type':'integer','minimum':capabilities['timeout_bounds']['min'],'maximum':capabilities['timeout_bounds']['max']}
    candidate=dossier['authority']['candidate']
    if candidate and dossier['trigger'].get('failed_job_id'):
        adjustments['resume']=obj({'job_id':enum([dossier['trigger']['failed_job_id']]),'head_oid':enum([candidate['head_oid']])})
    retry={'anyOf':[obj({key:adjustments[key] for key in keys}) for size in range(1,len(adjustments)+1) for keys in combinations(adjustments,size)]}
    targets=[obj({'repository_id':{'type':'integer','enum':[issue['repository_id']]},
                  'issue_number':{'type':'integer','enum':[issue['issue_number']]}})
             for issue in dossier['facts']['observations']['issues'] if issue['state']=='open']
    alternatives={'retry':retry,'drop':obj({'intent_note':string(8192)}),
                  'question':obj({'text':string()}),
                  'split':obj({'children':array(obj({'title':string(256),'body':string(8192),
                             'acceptance_criteria':array(string(),1,20)}),2,8),'intent_note':string(8192)})}
    if targets:
        alternatives['wait_on']={'anyOf':targets}
        alternatives['fold']={'anyOf':[obj({**target['properties'],'intent_note':string(8192)}) for target in targets]}
    base=dossier['facts']['observations']['base_oid']
    if base:alternatives['rebuild']=obj({'base_oid':enum([base])})
    signatures=dossier['facts']['forge_problem_signatures']
    if signatures:alternatives['escalate_forge']=obj({'signature':enum(signatures)})
    choices=array({'anyOf':[obj({'id':string(128),'action':enum([action]),'parameters':params,
                                  'reason':string(),'judgement':enum(['obvious','judgement'])})
                           for action,params in alternatives.items() if action in dossier['allowed_moves']]},1,4)
    reference_schema=obj({'job_id':string(256),'kind':string(100),'sequence':{'type':'integer','minimum':0},
                          'digest':{'type':'string','pattern':'^[0-9a-f]{64}$'}})
    refs=dossier['facts']['artifact_refs']
    if refs:reference_schema['enum']=refs
    evidence=array(reference_schema,0,20 if refs else 0)
    brief=obj({'choice_id':string(128),'instructions':string(8192),'evidence_refs':evidence})
    confidence={'type':'number','minimum':0,'maximum':1}
    recommended=string(128)
    if dossier['mode']=='brief':
        require(accepted is not None,'Brief schema requires the accepted decision')
        def literal(value):
            if isinstance(value,dict):return obj({key:literal(item) for key,item in value.items()})
            if isinstance(value,list):
                return {**array({'anyOf':[literal(item) for item in value]} if value else {},len(value),len(value)), 'enum':[value]}
            kind='boolean' if type(value) is bool else 'integer' if type(value) is int else 'number' if type(value) is float else 'null' if value is None else 'string'
            return {'type':kind,'enum':[value]}
        choices=literal(accepted['choices'])
        recommended=enum([accepted['recommended_choice_id']])
        confidence['enum']=[accepted['confidence']]
        brief['properties']['choice_id']=enum([dossier['trigger']['choice_id']])
    else:
        brief={'anyOf':[{'type':'null'},brief]}
    return obj({'version':{'type':'integer','enum':[1]},'dossier_id':enum([dossier['id']]),
                'dossier_digest':enum([dossier['digest']]),'choices':choices,
                'recommended_choice_id':recommended,'confidence':confidence,'brief':brief})
