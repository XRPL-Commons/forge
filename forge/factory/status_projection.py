"""One operator status for Activity, issue pages and client responses.

Lifecycle gates remain independent. This read projection names the selected
blocker separately from the broad state; it never changes scheduling authority.
"""
from ..store.status import describe

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
    if state in {'done', 'dropped', 'untracked'}:
        return {'done': 'completed', 'dropped': 'dropped', 'untracked': 'backlog'}[state]
    if reason in {'operator_pause', 'operator_backlog'}:
        return 'backlog'
    if reason in {'dependency', 'label_hold', 'ownership_conflict'}:
        return 'blocked'
    if reason in ATTENTION:
        return 'attention'
    return 'waiting' if reason else 'working' if active else 'queued'


def operator_status(issue, active=False):
    life = issue['lifecycle']
    code = public_status(issue, active)
    reason_code = life['reason_code']
    if code == 'completed':
        reason_code = 'merged' if life['stage_code'] == 'merged' else 'github_completed'
    elif code == 'dropped':
        reason_code = 'github_duplicate' if issue.get('state_reason') == 'duplicate' else 'github_not_planned'
    elif life['state'] == 'untracked':
        # Reopened issues must not inherit the old delivery's merge or gates.
        reason_code = None
    elif reason_code is None:
        reason_code = life['stage_code']
    return {'code':code, 'label':LABELS[code], 'reason_code':reason_code,
            'reason':describe(reason_code) if reason_code else 'Not queued for Forge'}
