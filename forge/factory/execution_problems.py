"""Deterministic execution policy from private envelopes and host observations."""
import re
from ..harnesses.execution import fault, valid


def classify(job, capture, now):
    from .startup_problems import problem
    startup = problem(job, capture)
    if startup is not None:
        return dict(kind='forge_problem', details={**startup, 'source':'execution'})
    execution = capture.get('execution') or {}
    # Existing images keep their startup proof; protocol v1 is activated only
    # by a qualified new image, never retroactively inferred from a report.
    if execution.get('exit_protocol') != '1':
        return None
    image = execution.get('image_id')
    exact = (execution.get('id') == job.get('container_id') and job.get('container_id')
             and isinstance(image, str) and re.fullmatch(r'sha256:[0-9a-f]{64}', image))
    if not exact:
        return None
    common = dict(source='execution', owner='forge', image_id=image, job_id=job['id'], capture_job_id=job['id'])
    if execution.get('oom_killed') is True:
        return dict(kind='oom', details={**common, 'reason': 'resources', 'oom_killed': True})
    if capture.get('timed_out') is True:
        return dict(kind='timeout', details={**common, 'reason': 'timeout'})
    if execution.get('running') is not False:
        return None
    envelope = capture.get('execution_envelope')
    if not valid(envelope, job['id'], final=True) or envelope['exit_code'] != execution.get('exit_code'):
        detail = fault('MissingExecutionEnvelope', phase='runner', file='/opt/forge/job.py')
        return dict(kind='forge_problem', details={**common, **detail, 'reason': 'execution_envelope_missing'})
    status = envelope['harness_status']
    if status == 'timeout':
        return dict(kind='timeout', details={**common, 'reason': 'timeout'})
    if status == 'harness_error':
        return dict(kind='forge_problem', details={**common, **envelope['harness_error'], 'reason': 'harness_error'})
    if status == 'provider_error':
        provider = envelope['provider_error']
        return dict(kind='provider_error', details={**common, 'reason': 'provider_' + provider['code'],
            'provider_error': provider, 'retry_at': now + provider.get('retry_after', 30)})
    return dict(kind='ok', details=common)


def contract_problem(job, capture):
    """Call only after the host failed result contract validation."""
    decision = classify(job, capture, 0)
    if decision is None or decision['kind'] != 'ok':
        return None
    return {**decision['details'], **fault('InvalidResultContract', phase='contract'), 'reason': 'result_contract'}
