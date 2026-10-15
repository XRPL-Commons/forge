"""Shared planning action contracts for the server, CLI and isolated maintenance."""
from copy import deepcopy


def object_schema(properties, *, required=None, **metadata):
    return {'type':'object','additionalProperties':False,'properties':properties,
            'required':list(properties) if required is None else required,**metadata}


def text_schema(limit=None, *, empty=False, **metadata):
    result={'type':'string'}
    if not empty:result.update(minLength=1,pattern=r'\S')
    if limit is not None:
        result.update(maxLength=limit,**{'x-max-utf8-bytes':limit})
    result.update(metadata)
    return result


def action_schemas():
    identifier=text_schema(256)
    request={**identifier,'description':'Stable identity for replaying this exact operation. A different payload or actor requires a new identity.'}
    author=object_schema({'id':{'type':'integer','minimum':1},'type':{'enum':['User','Bot']},'login':text_schema()})
    source=object_schema({'url':text_schema(2048,format='uri',pattern=r'^https://[^/@?#]+(?:[/?#]|$)',description='HTTPS URL without embedded credentials'),
        'description':text_schema(1000),'kind':{'enum':['issue_revision','candidate','commit','decision','document']},'identity':identifier})
    sources={'type':'array','items':source,'minItems':1,'maxItems':20,'x-max-json-utf8-bytes':8192}
    existing_pr=object_schema({'unit_id':text_schema(),'candidate_id':text_schema(),'pull_number':{'type':'integer','minimum':1},
                               'head_oid':text_schema(),'author':author})
    existing_target=object_schema({'kind':{'const':'existing'},**deepcopy(existing_pr['properties']),'ownership_transfer':{'const':True}},
        description='Matches one exact existing_prs receipt, including its author. Approval transfers implementation to the delivery builder.')
    labels={'type':'array','maxItems':25,'items':{'type':'string','minLength':1,'maxLength':50}}
    parameters={
        'close':object_schema({'evidence':sources}),
        'relabel':object_schema({'add':labels,'remove':deepcopy(labels)}),
        'park':object_schema({'add':labels}),
        'group':object_schema({'dependencies':{'type':'array','items':object_schema({'issue_id':text_schema(),'prerequisite_issue_id':text_schema()})},
            'existing_prs':{'type':'array','items':existing_pr},
            'publication_target':{'oneOf':[object_schema({'kind':{'const':'new'}}),existing_target]}},
            description='Dependencies name distinct members in this row. Include every current source PR exactly once; the server verifies its pinned head and author.'),
        'order':object_schema({'prerequisites':{'type':'array','uniqueItems':True,'items':{'type':'string','pattern':r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*$'}}}),
        'question':object_schema({'question':text_schema(4000)}),
        'keep':object_schema({}),
    }
    rows=[]
    for action,params in parameters.items():
        params['x-max-json-utf8-bytes']=65536
        members={'type':'array','items':text_schema(),'uniqueItems':True,'minItems':2 if action=='group' else 1}
        if action!='group':members['maxItems']=1
        rows.append(object_schema({'action':{'const':action},'members':members,'reason':text_schema(8192,description='Explain the proposed action and its evidence. Approval and application are separate; do not report this proposal as an executed change.'),
            'rule_quote':{'oneOf':[{'type':'null'},text_schema(16384)],'description':'Exact quote from the proposal’s pinned rules, or null.'},
            'parameters':params}))
    common={'request_id':request}
    operations={
        'triage':object_schema({'action':{'const':'triage'},**common},description='Request a proposal; issue mutations require later explicit acceptance.'),
        'rules':object_schema({'action':{'const':'rules'},**common,'expected_revision':{'type':'integer','minimum':0},
            'rules':text_schema(16384,empty=True),'proposals_enabled':{'type':'boolean'}},
            description='Compare-and-set repository planning rules and automatic proposal scheduling. Use revision 0 for a new planning configuration.'),
        'note':object_schema({'action':{'const':'note'},**common,'note_id':identifier,'expected_revision':{'type':'integer','minimum':0},
            'kind':{'enum':['convention','decision','lesson','question','next_action']},'content':text_schema(8192),
            'sources':sources,'deleted':{'type':'boolean'}},
            description='Append a note revision. Use revision 0 for a new note. Archiving with deleted=true still requires content and sources.'),
        'edit':object_schema({'action':{'const':'edit'},**common,'proposal_id':text_schema(),'row_id':text_schema(),
            'expected_version':{'type':'integer','minimum':1},'value':{'oneOf':rows}},
            description='Replace one undecided row using the current proposal version. Preserve that row’s exact member set; membership changes require a new complete proposal.'),
        'choose':object_schema({'action':{'const':'choose'},**common,'proposal_id':text_schema(),'row_id':text_schema(),
            'expected_version':{'type':'integer','minimum':1},'choice_id':text_schema(128),'digest':text_schema(64)},
            description='Select one captured ranked alternative by exact identity. This updates the proposal row; acceptance remains a separate explicit operation.'),
        'accept':object_schema({'action':{'const':'accept'},**common,'proposal_id':text_schema(),'expected_version':{'type':'integer','minimum':1},
            'accept_rows':{'type':'array','items':text_schema(),'uniqueItems':True},
            'reject_rows':{'type':'array','items':text_schema(),'uniqueItems':True}},
            anyOf=[{'properties':{'accept_rows':{'minItems':1}}},{'properties':{'reject_rows':{'minItems':1}}}],
            description='Explicitly approve or reject selected rows at the current proposal version. The arrays must be disjoint. Approval validates fresh issue and PR evidence before one atomic transaction.'),
    }
    return operations


def provider_schema(value):
    """Translate canonical CLI schema keywords for runner structured output."""
    if isinstance(value,list):return [provider_schema(item) for item in value]
    if not isinstance(value,dict):return value
    result={}
    for key,item in value.items():
        if key.startswith('x-') or key in {'uniqueItems','format'}:continue
        if key=='const':result.update(type='boolean' if isinstance(item,bool) else 'string',enum=[item])
        else:result['anyOf' if key=='oneOf' else key]=provider_schema(item)
    if 'enum' in result and 'type' not in result:result['type']='string'
    return result


def provider_triage_rows(*, rules):
    """Generate runner rows from the same action contract exposed by the CLI.

    Provider structured output uses anyOf/enum rather than oneOf/const and
    leaves uniqueness, URI and encoded-byte checks to canonical host validation.
    """
    rows=provider_schema(action_schemas()['edit']['properties']['value'])
    for branch in rows['anyOf']:
        if not rules:branch['properties']['rule_quote']={'type':'null'}
        branch['properties']['rule_quote']['description']=(
            'Exact substring of context.rules that decides this action; null if no rule applies.')
    return rows


def commands():
    target={'repository':{'type':'string','pattern':r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$','description':'GitHub OWNER/REPO'},
            'server':{'type':'string','format':'uri','pattern':'^https://','description':'Previously connected HTTPS origin'}}
    actions=action_schemas()
    return {
        'triage':{'usage':'forge triage OWNER/REPO --server HTTPS_ORIGIN --request-id STABLE_ID --json',
            'capability':'repositories:triage',
            'input_schema':object_schema({**target,'request_id':deepcopy(actions['triage']['properties']['request_id'])})},
        'planning':{'usage':'forge planning OWNER/REPO --server HTTPS_ORIGIN [--proposal ID] [--row ID] [--after CURSOR] [--action-file PATH] --json',
            'read_capability':'repositories:read','write_capability':'repositories:triage',
            'input_schema':object_schema({**target,'proposal':text_schema(description='Proposal to read; rows are paginated.'),
                'row':text_schema(description='Row whose complete member list to read; takes precedence over proposal view.'),
                'after':{'type':'string','description':'Exact returned cursor: proposals.next for repository history, proposal.next for rows, or members.next for a row’s members.'},
                'action_file':text_schema(description='Path to one JSON operation matching action_file_schema. Takes precedence over read selectors.')},required=['repository','server']),
            'action_file_schema':{'$schema':'https://json-schema.org/draft/2020-12/schema','oneOf':list(actions.values())},
            'operations':{name:{'description':schema['description'],'schema_pointer':f'/oneOf/{index}'} for index,(name,schema) in enumerate(actions.items())},
            'limits':{'request_json_bytes':16384,'text_limits':'x-max-utf8-bytes counts encoded UTF-8 bytes; x-max-json-utf8-bytes counts serialized JSON bytes.'},
            'replay':'Reuse request_id only for the exact original action file. After a version conflict, read current state and submit a reviewed decision with a new request_id.'},
    }
