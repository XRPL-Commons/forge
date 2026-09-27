"""Bounded memory retries. Admission still reserves each container's full limit."""
import re
from .capacity import memory_bytes
from .contracts import NEXT_ROLE, result


def profile_key(repo_id, role):
    return f'{repo_id}:{role}'


def confirmed(capture, report=None):
    if (capture.get('execution') or {}).get('oom_killed'):
        return True
    # Generic resources blockers (disk, CPU, process limits) are not memory proof.
    return bool(report and report.get('outcome') == 'blocked_environment'
                and report.get('blocker') == 'resources'
                and re.search(r'out[- ]of[- ]memory|heap exhaustion|heap limit|memory limit|OOM[- ]kill',
                              report.get('summary', ''), re.I))


def allocation(config, data, item, role):
    tiers = config.get('memory_tiers', [])
    if not tiers:
        return config['memory']
    preferred = data.get('memory_profiles', {}).get(profile_key(item['repo_id'], role), {}).get('memory', tiers[0])
    retry = item.get('memory_retry') or {}
    required = retry.get('memory', tiers[0]) if retry.get('role') == role else tiers[0]
    # Existing evidence survives turning this policy on or restarting it.
    # Do not repeat a known-small allocation for an issue already attempted.
    for job in data['jobs'].values():
        if job['issue_key'] != item['key'] or job['role'] != role:
            continue
        capture = job.get('capture') or {}
        try:
            report = result(capture.get('result'), job)
        except ValueError:
            report = None
        if confirmed(capture, report):
            used = (job.get('runner_config') or {}).get('memory', tiers[0])
            larger = next((v for v in tiers if memory_bytes(v) > memory_bytes(used)), tiers[-1])
            if memory_bytes(larger) > memory_bytes(required):
                required = larger
    size = max(memory_bytes(preferred), memory_bytes(required))
    return next((tier for tier in tiers if memory_bytes(tier) >= size), tiers[-1])


def pending(data, config):
    """Oldest runnable escalation; blocked/cancelled issues never reserve capacity."""
    if not config.get('memory_tiers'):
        return []
    rows = []
    for item in data['issues'].values():
        retry = item.get('memory_retry')
        if (not retry or item.get('active') or item.get('environment_hold') or item.get('operator_request')
                or item.get('operator_hold') or item['state'] in ('hold', 'failed', 'merged', 'cancelled')
                or any(j['issue_key'] == item['key'] and not j['cleanup'] for j in data['jobs'].values())):
            continue
        role = retry['role']
        if not config['slots'].get(role):
            continue
        if NEXT_ROLE.get(item['state']) == role or (role == 'recovery' and any(
                f['investigation'] == 'pending' for f in item['failures'])):
            rows.append(item)
    return sorted(rows, key=lambda item: (item['memory_retry']['at'], item['key']))
