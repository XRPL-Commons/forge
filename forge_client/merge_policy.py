"""Deterministic repository policy inspection and compare-and-set."""
import uuid

from .contracts import require


def execute(client,target,mode=None,*,revision=None,request_id=None,github_protection=None):
    require(mode in (None,'auto','human'))
    require(github_protection in (None,'required','optional'))
    require(mode is None or github_protection is None, 'invalid_policy', 'Change one repository policy at a time.')
    selector = {'policy': 'github_protection'} if github_protection is not None else {}
    if github_protection is not None: mode = github_protection
    if mode is None:
        require(revision is None and request_id is None,'mode_required','Choose auto or human before supplying write options.')
        return client.request('policy',target,action='read',**selector)
    if revision is None:
        revision=client.request('policy',target,action='read',**selector)['revision']
    return client.request('policy',target,action='set',mode=mode,revision=revision,
                          request_id=request_id or uuid.uuid4().hex,**selector)
