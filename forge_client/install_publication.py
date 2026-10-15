"""Signing probes use the same durable request identity after connection loss."""
from .contracts import digest


def qualify(factory, state, target, *, apply=False):
    probe = state['smoke_targets']['publication']['target']
    return factory.call('publication', repository={key: target[key] for key in ('id', 'name')},
                        probe_repository={key: probe[key] for key in ('id', 'name')},
                        request_id=digest({'installation': state['inputs_digest'],
                                           'publication': target['id']})[:32], apply=apply)
