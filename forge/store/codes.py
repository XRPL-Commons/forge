"""The closed code registry: unit states and transitions, gate codes and close codes.

Every lifecycle fact the store records is one of these codes. Wording lives in
`status.describe()`; nothing compares prose. The first migration seeds the same
registry into reference tables so the database rejects an unknown code too, and
`tests/test_store_codes.py` checks that both copies agree. Changing a code here needs
a new migration.
"""
from __future__ import annotations

from dataclasses import dataclass

# --- Units -----------------------------------------------------------------

# A state names the phase, including time waiting for a worker. It never asserts
# that a worker is running; current jobs say that.
UNIT_STATES = ('queued', 'building', 'integrating', 'reviewing', 'auditing',
               'merging', 'merged', 'closed', 'dissolved')
TERMINAL_STATES = frozenset({'merged', 'closed', 'dissolved'})
OPEN_STATES = tuple(state for state in UNIT_STATES if state not in TERMINAL_STATES)
REWORK_STATES = ('building', 'integrating')
CANDIDATE_STATES = frozenset({'reviewing', 'auditing', 'merging'})

TRANSITIONS: dict[str, frozenset[str]] = {
    # Reserve the first build part.
    'queued': frozenset({'building', 'reviewing', 'closed', 'dissolved'}),
    # Finish all parts (batch), or publish a singleton candidate directly.
    'building': frozenset({'integrating', 'reviewing', 'closed', 'dissolved'}),
    # Publish the combined candidate; a dropped member's descendants rebuild.
    'integrating': frozenset({'reviewing', 'building', 'closed', 'dissolved'}),
    # Review passes, or rejects (rework), or a human merged the candidate.
    'reviewing': frozenset({'auditing', 'building', 'integrating', 'merged', 'closed', 'dissolved'}),
    'auditing': frozenset({'merging', 'building', 'integrating', 'merged', 'closed', 'dissolved'}),
    # A base conflict or candidate change rebuilds at the current base.
    'merging': frozenset({'merged', 'building', 'integrating', 'closed', 'dissolved'}),
    'merged': frozenset(),
    'closed': frozenset(),
    'dissolved': frozenset(),
}


@dataclass(frozen=True)
class CloseCode:
    """Why delivery ended without a merge, and what that means for each member issue."""
    issue_outcome: str | None  # 'done', 'dropped', or None when the issue continues elsewhere


CLOSE_CODES: dict[str, CloseCode] = {
    # Operator decision (2026-10-05): closed as completed elsewhere shows as done.
    'github_completed': CloseCode('done'),
    'github_not_planned': CloseCode('dropped'),
    'github_duplicate': CloseCode('dropped'),
    # The operator ended this delivery; the GitHub issue may stay open.
    'operator_closed': CloseCode(None),
    # Retired after its members moved into a combined unit.
    'combined': CloseCode(None),
}

# Membership exit reasons, kept with history when a member leaves a unit.
EXIT_REASONS = ('dropped', 'dissolved', 'merged', 'closed', 'combined')
PART_STATES = ('pending', 'building', 'built', 'failed', 'removed', 'done')

# Projected issue states (#209). Derived from units and GitHub facts, never stored.
ISSUE_STATES = ('untracked', 'queued', 'in_unit', 'blocked', 'done', 'dropped')

# --- Gates -----------------------------------------------------------------

# How a gate may be released. A resolution names its rule; a different rule is
# refused, so "recheck environment" cannot release an operator pause.
RELEASE_RULES = {
    'operator': 'An explicit operator decision',
    'observation': 'A fresh authoritative observation that the cause is gone',
    'recheck': 'An operator-requested recheck that proves the repair',
    'time': 'The retry time passing',
    'evidence': 'Fresh evidence bound to the current candidate',
    'transition': 'The lifecycle transition that completes the pending work',
}

# Presentation precedence, from the design's "one state, one current blocker".
GATE_KINDS = {
    'intent': 2,      # explicit intent and ownership protections
    'decision': 3,    # an operator decision is required before new code work
    'readiness': 4,   # admission, capabilities or evidence unavailable
    'dependency': 5,  # unmet external prerequisites
    'automatic': 6,   # automatic recovery or scheduling waits
    'candidate': 7,   # candidate gates for the current phase
}


@dataclass(frozen=True)
class Gate:
    kind: str
    release: str
    scopes: frozenset[str]  # 'unit', 'member' and/or an exact 'candidate'

    @property
    def priority(self):
        return GATE_KINDS[self.kind]

    @property
    def candidate(self):
        """Candidate gates bind to one exact candidate and lapse with it."""
        return self.kind == 'candidate'


UNIT = frozenset({'unit'})
EITHER = frozenset({'unit', 'member'})
CANDIDATE = frozenset({'candidate'})
ANY_SCOPE = EITHER | CANDIDATE

GATES: dict[str, Gate] = {
    'operator_pause': Gate('intent', 'operator', UNIT),
    'operator_backlog': Gate('intent', 'operator', UNIT),
    # Operator decision (2026-10-05): removing the hold label resumes the work.
    'label_hold': Gate('intent', 'observation', EITHER),
    'membership_decision': Gate('intent', 'operator', EITHER),
    'operator_sync': Gate('intent', 'observation', UNIT),
    'external_change': Gate('intent', 'operator', EITHER),
    'ownership_conflict': Gate('intent', 'observation', EITHER),
    'budget_exhausted': Gate('decision', 'operator', UNIT),
    'approach_required': Gate('decision', 'operator', EITHER),
    'decision_required': Gate('decision', 'operator', EITHER),
    # Operator decision (2026-10-05): work resumes when repository setup is ready.
    'repository_setup': Gate('readiness', 'observation', UNIT),
    'environment': Gate('readiness', 'recheck', EITHER),
    'observation_unavailable': Gate('readiness', 'observation', ANY_SCOPE),
    'legacy_reconciliation': Gate('readiness', 'operator', EITHER),
    'dependency': Gate('dependency', 'observation', EITHER),
    'recovery_pending': Gate('automatic', 'transition', ANY_SCOPE),
    'cleanup_pending': Gate('automatic', 'transition', ANY_SCOPE),
    'result_pending': Gate('automatic', 'transition', ANY_SCOPE),
    'provider_unavailable': Gate('automatic', 'time', UNIT),
    'capacity': Gate('automatic', 'transition', UNIT),
    'retry_delay': Gate('automatic', 'time', EITHER),
    'verification_human': Gate('candidate', 'evidence', CANDIDATE),
    'verification_ci': Gate('candidate', 'evidence', CANDIDATE),
    'approval_required': Gate('candidate', 'evidence', CANDIDATE),
    'changes_requested': Gate('candidate', 'transition', CANDIDATE),
    'merge_checks': Gate('candidate', 'observation', CANDIDATE),
    'merge_conflict': Gate('candidate', 'transition', CANDIDATE),
}

# --- Jobs, artifacts and events --------------------------------------------

JOB_ROLES = ('build', 'integration', 'review', 'audit', 'recovery', 'merge')
JOB_PHASES = ('reserved', 'starting', 'running', 'collecting', 'finished', 'cleaned')
LIVE_JOB_PHASES = ('reserved', 'starting', 'running', 'collecting')

# Raw logs expire after 30 days (operator decision, 2026-10-05). Results,
# summaries and input manifests are durable facts and never expire.
RAW_LOG_RETENTION_DAYS = 30
ARTIFACT_KINDS = {
    'logs': 'raw',
    'output': 'raw',
    'live_tail': 'raw',
    'ci_transcript': 'raw',
    'launch_input': 'durable',
    'failure_evidence': 'durable',
    'worker_capture': 'durable',
    'input_manifest': 'durable',
    'result': 'durable',
    'summary': 'durable',
}

EVENT_KINDS = frozenset({
    'legacy_event', 'execution_progress',
    'unit_created', 'admitted', 'membership_changed', 'part_accepted',
    'candidate_selected', 'candidate_superseded', 'phase_changed',
    'gate_opened', 'gate_resolved', 'attempt_reserved', 'attempt_finished',
    'attempt_fenced', 'lease_released', 'budget_charged', 'budget_corrected',
    'budget_reset', 'decision_applied', 'decision_rejected', 'merged',
    'closed', 'dissolved', 'issue_observed', 'issue_admitted', 'lifecycle_changed', 'cleanup_verified',
})
