"""Strict decision fixtures with real captured dossier identity."""
from copy import deepcopy


def output(dossier,choices,*,reply=None):
    result=dict(version=1,dossier_id=dossier['id'],dossier_digest=dossier['digest'],choices=deepcopy(choices),
        recommended_choice_id=choices[0]['id'],confidence=.8,brief=None)
    if dossier['mode']=='conversation':result['metadata']=dict(reply=reply or 'Read the captured evidence.',notes=[],handoff=None)
    return result


def choice(action,parameters=None,*,identity='first',reason='Evidence supports this choice.'):
    return dict(id=identity,action=action,parameters=parameters or {},reason=reason,judgement='judgement')


def row_choice(action,member,parameters=None,*,identity='first'):
    return choice(action,dict(members=[member],rule_quote=None,details=parameters or {}),identity=identity)
