"""One operator status for Activity, issue pages and client responses.

Lifecycle gates remain independent. This read projection names the selected
blocker separately from the broad state; it never changes scheduling authority.
"""
import json

from ..store.codes import transitional
from ..store.status import describe, describe_gate, github_rule

LABELS = {
    'backlog': 'Backlog', 'queued': 'Queued', 'working': 'Working',
    'attention': 'Needs attention', 'waiting': 'Waiting', 'blocked': 'Blocked',
    'completed': 'Completed', 'dropped': 'No longer needed',
}
ATTENTION = {'budget_exhausted', 'approach_required', 'decision_required', 'repository_setup', 'environment',
             'legacy_reconciliation', 'membership_decision', 'external_change', 'verification_human',
             'approval_required', 'observation_unavailable'}


def public_status(issue, active=False):
    state, reason = issue['lifecycle']['state'], issue['lifecycle']['reason_code']
    if state == 'untracked' and reason in {'requirements_stale', 'remote_timestamp_invalid', 'admission_conflict'}:
        return 'attention'
    if state in {'done', 'dropped', 'untracked'}:
        return {'done': 'completed', 'dropped': 'dropped', 'untracked': 'backlog'}[state]
    if reason in {'operator_pause', 'operator_backlog'}:
        return 'backlog'
    if reason in {'dependency', 'label_hold', 'ownership_conflict'}:
        return 'blocked'
    if reason in ATTENTION:
        return 'attention'
    # Capturing a result or cleaning up is the stage still at work, not a wait.
    if transitional(reason):
        return 'working'
    return 'waiting' if reason else 'working' if active else 'queued'


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
    blocker = life.get('current_blocker')
    if blocker and blocker['code'] == reason_code:
        reason = describe_gate(blocker)
        if reason_code == 'environment':
            details = json.loads(blocker['details_json'])
            # Its own reason: a GitHub rule, not a build environment to repair.
            if github_rule(details.get('value', details)): reason_code = 'github_permission'
    else:
        reason = describe(reason_code) if reason_code else 'Not queued for Forge'
    return {'code':code, 'label':LABELS[code], 'reason_code':reason_code, 'reason':reason}
