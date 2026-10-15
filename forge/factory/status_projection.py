"""One operator status for Activity, issue pages and client responses.

Lifecycle gates remain independent. This read projection names the selected
blocker separately from the broad state; it never changes scheduling authority.
"""
import json

from ..store.codes import GATES
from ..store.status import describe, describe_gate, github_rule

LABELS = {
    'backlog': 'Backlog', 'queued': 'Queued', 'working': 'Working',
    'attention': 'Needs attention', 'waiting': 'Waiting', 'blocked': 'Blocked',
    'completed': 'Completed', 'dropped': 'No longer needed',
}


SCHEDULE_REASONS = {
    'admitted': 'Scheduled to start', 'operator_recheck': 'Scheduled to check readiness again',
    'role_capacity': 'Waiting for a free worker for this stage',
    'capacity_unavailable': 'Waiting for enough worker resources',
    'provider_unavailable': 'Waiting for an available coding account',
    'runtime_disabled': 'Scheduling is paused for maintenance',
    'repository_setup': 'Checking repository readiness',
    'observation_changed': 'Refreshing the issue before starting',
    'base_changed': 'Rebuilding on the updated base branch',
    'candidate_changed': 'Refreshing the pull request before continuing',
    'authorization': 'Checking merge approval and required checks',
    'mergeability_unknown': 'Waiting for GitHub to establish whether the pull request can merge',
    'merge_conflict': 'Confirming a merge conflict before rebuilding',
    'human_approval': 'Checking for human approval',
    'technical_evidence': 'Waiting for Review and Audit evidence',
    'cleanup_pending': 'Waiting for worker cleanup', 'cleanup': 'Waiting for worker cleanup',
    'issue_reconciliation': 'Refreshing the issue after the worker finished',
    'policy_reconciliation': 'Refreshing the repository rules',
    'candidate_reconciliation': 'Refreshing the completed pull request',
    'base_reconciliation': 'Checking the target branch after the worker finished',
    'verdict_effect': 'Confirming the published review',
    'recovery_controller': 'Scheduled for recovery',
    'github_budget': 'Continuing on the next scheduler turn',
}


def public_status(issue, active=False):
    state, reason = issue['lifecycle']['state'], issue['lifecycle']['reason_code']
    if state == 'untracked' and reason in {'requirements_stale', 'remote_timestamp_invalid', 'admission_conflict'}:
        return 'attention'
    if state in {'done', 'dropped', 'untracked'}:
        return {'done': 'completed', 'dropped': 'dropped', 'untracked': 'backlog'}[state]
    if reason in GATES:
        return GATES[reason].public_status
    return 'waiting' if reason else 'working' if active else 'queued' if issue['lifecycle'].get('next_action') else 'waiting'


def operator_status(issue, active=False):
    life = issue['lifecycle']
    code = public_status(issue, active)
    reason_code = life['reason_code']
    if code == 'completed':
        reason_code = 'merged' if life['stage_code'] == 'merged' else 'github_completed'
    elif code == 'dropped':
        reason_code = 'github_duplicate' if issue.get('state_reason') == 'duplicate' else 'github_not_planned'
    elif life['state'] == 'untracked' and reason_code is None:
        # Reopened issues must not inherit the old delivery's merge or gates.
        reason_code = None
    elif reason_code is None:
        reason_code = life['stage_code']
    action = life.get('next_action')
    blocker = life.get('current_blocker')
    if blocker and blocker['code'] == reason_code:
        reason = describe_gate(blocker)
        if reason_code == 'environment':
            details = json.loads(blocker['details_json'])
            # Its own reason: a GitHub rule, not a build environment to repair.
            if github_rule(details.get('value', details)): reason_code = 'github_permission'
    else:
        reason = describe(reason_code) if reason_code else 'Not queued for Forge'
    if code == 'queued' and action:
        reason = SCHEDULE_REASONS.get(action['reason'], 'Forge will check readiness again')
        reason_code = action['reason']
    elif code == 'waiting' and not life['reason_code'] and not action:
        reason = 'Waiting for the scheduler to assign the next action'
    return {**({'next_action': {key:action[key] for key in ('owner','reason','due_at')}} if action else {}), 'code':code, 'label':LABELS[code], 'reason_code':reason_code, 'reason':reason, **({'owner':'forge'} if reason_code=='forge_problem' else {})}
