"""Exact approval payloads for delivery membership changes."""
import argparse
import json
import uuid
from pathlib import Path

from .contracts import require


def add_parser(sub):
    group=sub.add_parser('delivery',help='Inspect, remove members, close selected issues, or dissolve an approved delivery')
    commands=group.add_subparsers(dest='delivery_command',required=True)
    for action in ('inspect','drop','close-member','dissolve','resume-delivery'):
        command=commands.add_parser(action)
        command.add_argument('repository',help='GitHub OWNER/REPO')
        command.add_argument('key',help='Exact Forge issue key from forge repo issues')
        command.add_argument('--server',required=True)
        command.add_argument('--json',action='store_true',default=argparse.SUPPRESS)
        if action!='inspect':
            command.add_argument('--decision',required=True,help='JSON file containing the exact inspect response to approve')
            command.add_argument('--reason',required=True)
            command.add_argument('--apply',action='store_true',required=True,help='Approve the described membership change and dependent choices')
            command.add_argument('--request-id',help='Canonical UUID to replay this exact decision')


def commands():
    result={}
    for action in ('inspect','drop','close-member','dissolve','resume-delivery'):
        properties={'repository':{'type':'string','description':'GitHub OWNER/REPO'},
                    'key':{'type':'string','description':'Exact issue key from repo issues'},
                    'server':{'type':'string','format':'uri'}}
        required=list(properties)
        if action!='inspect':
            properties.update(decision={'type':'string','description':'Path to the exact inspect JSON approval'},
                              reason={'type':'string','minLength':1,'maxLength':2000},
                              apply={'type':'boolean','const':True},request_id={'type':'string','format':'uuid'})
            required+=['decision','reason','apply']
        result[action]={'usage':f'forge delivery {action} OWNER/REPO ISSUE_KEY --server HTTPS_ORIGIN'+('' if action=='inspect' else ' --decision FILE --reason TEXT --apply'),
                        'capability':'repositories:read' if action=='inspect' else 'repositories:triage',
                        'input_schema':{'type':'object','additionalProperties':False,'properties':properties,'required':required},
                        'changes_shared_budget':action=='dissolve','closes_shared_pr':False}
    return result


def execute(client,target,args):
    if args.delivery_command=='inspect':return client.request('delivery',target,action='inspect',key=args.key)
    value=json.loads(Path(args.decision).read_text())
    require(isinstance(value,dict) and isinstance(value.get('delivery'),dict) and isinstance(value.get('revision'),str),
            'decision_required','Save the inspect response, review its members and dependent choices, then approve that exact file.')
    return client.request('delivery',target,action=args.delivery_command.replace('-','_'),key=args.key,
                          revision=value['revision'],membership=value['delivery']['decision'],reason=args.reason,
                          request_id=args.request_id or str(uuid.uuid4()))
