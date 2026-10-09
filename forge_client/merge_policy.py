"""Deterministic repository policy inspection and compare-and-set."""
import uuid

from .contracts import require


def execute(client,target,mode=None,*,revision=None,request_id=None):
    require(mode in (None,'auto','human'))
    if mode is None:
        require(revision is None and request_id is None,'mode_required','Choose auto or human before supplying write options.')
        return client.request('policy',target,action='read')
    if revision is None:
        revision=client.request('policy',target,action='read')['revision']
    return client.request('policy',target,action='set',mode=mode,revision=revision,
                          request_id=request_id or uuid.uuid4().hex)
