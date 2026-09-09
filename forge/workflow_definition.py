"""Issue lifecycle policy, independent of worker/process/slot lifecycles."""
from types import MappingProxyType

MAX_REJECTED_CYCLES = 3
PR_LABELS = MappingProxyType({'ready-for-review':'review', 'ready-for-audit':'audit', 'changes-requested':'build'})
QUEUES = MappingProxyType({'sentback':'build', 'limbo':'build', 'review':'review', 'audit':'audit'})
STAGES = frozenset({'queued', 'build', 'review', 'audit', 'merge', 'failed', 'done', 'cancelled', 'unknown'})
TERMINAL = frozenset({'done', 'cancelled'})
TRANSITIONS = MappingProxyType({
    ('queued', 'dispatch'): 'build',
    ('build', 'pass'): 'review',
    ('review', 'pass'): 'audit',
    ('review', 'fail'): 'build',
    ('audit', 'fail'): 'build',
    ('audit', 'pass'): 'merge',
    ('merge', 'completed'): 'done',
    ('failed', 'retry'): 'build',
})


def next_stage(stage, event, *, rejected_cycles=0):
    target = TRANSITIONS[(stage, event)]
    if event == 'fail' and rejected_cycles >= MAX_REJECTED_CYCLES:
        return 'failed'
    return target


def dispatchable(stage, disposition, role):
    return disposition == 'pending' and stage in ({'queued', 'build'} if role == 'build' else {role})


def definition():
    return {'max_rejected_cycles': MAX_REJECTED_CYCLES,
            'transitions': [{'from': source, 'event': event, 'to': target}
                            for (source, event), target in TRANSITIONS.items()],
            'exhaustion': {'event': 'fail', 'to': 'failed'},
            'completion': 'GitHub issue closed as completed'}
