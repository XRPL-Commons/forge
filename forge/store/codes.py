"""The code registry Python reads: unit states, gate codes and close codes.

Wording lives in `status.describe()`; nothing compares prose. The schema seeds
the full registry (including unit transitions, job phases and event kinds) into
reference tables so the database rejects an unknown code, and
`tests/test_store_codes.py` checks that the codes here agree with it. Changing a
code needs a new migration.
"""
from __future__ import annotations

from dataclasses import dataclass

# --- Units -----------------------------------------------------------------

# A state names the phase, including time waiting for a worker. It never asserts
# that a worker is running; current jobs say that.
UNIT_STATES = ('queued', 'building', 'integrating', 'reviewing', 'auditing',
               'merging', 'merged', 'closed', 'dissolved')
TERMINAL_STATES = frozenset({'merged', 'closed', 'dissolved'})


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
    # An imported delivery retired by migration 041; discovery admits its open
    # issue again as a new delivery.
    'import_superseded': CloseCode(None),
}

# --- Gates -----------------------------------------------------------------

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
    'operator_pause': Gate('intent', 'operator', EITHER),
    'operator_backlog': Gate('intent', 'operator', EITHER),
    # Operator decision (2026-10-05): removing the hold label resumes the work.
    'label_hold': Gate('intent', 'observation', EITHER),
    'membership_decision': Gate('intent', 'operator', EITHER),
    'operator_sync': Gate('intent', 'observation', EITHER),
    'triage_sync': Gate('intent', 'observation', frozenset({'member'})),
    'external_change': Gate('intent', 'observation', EITHER),
    'ownership_conflict': Gate('intent', 'observation', EITHER),
    'budget_exhausted': Gate('decision', 'operator', UNIT),
    'approach_required': Gate('decision', 'operator', EITHER),
    'decision_required': Gate('decision', 'operator', EITHER),
    # Operator decision (2026-10-05): work resumes when repository setup is ready.
    'forge_problem': Gate('readiness', 'observation', ANY_SCOPE | frozenset({'job'})),
    'repository_setup': Gate('readiness', 'observation', UNIT),
    'environment': Gate('readiness', 'recheck', EITHER),
    'observation_unavailable': Gate('readiness', 'observation', ANY_SCOPE),
    'legacy_reconciliation': Gate('readiness', 'operator', EITHER),
    'dependency': Gate('dependency', 'observation', EITHER),
    'recovery_pending': Gate('automatic', 'transition', ANY_SCOPE | frozenset({'job'})),
    'cleanup_pending': Gate('automatic', 'transition', ANY_SCOPE | frozenset({'job'})),
    'result_pending': Gate('automatic', 'transition', ANY_SCOPE | frozenset({'job'})),
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

# How an open gate reads to the operator (#241). A transitional gate is the
# current stage settling (capturing a result, cleaning up, routing a failure):
# it shows as work in progress at that stage. A blocking gate needs something
# outside the pipeline and is a hold. Every code is listed; unknown is blocking.
TRANSITIONAL, BLOCKING = 'transitional', 'blocking'
GATE_PRESENTATION: dict[str, str] = {
    'operator_pause': BLOCKING, 'operator_backlog': BLOCKING, 'label_hold': BLOCKING,
    'membership_decision': BLOCKING, 'operator_sync': BLOCKING, 'triage_sync': BLOCKING,
    'external_change': BLOCKING, 'ownership_conflict': BLOCKING, 'budget_exhausted': BLOCKING,
    'approach_required': BLOCKING, 'decision_required': BLOCKING, 'repository_setup': BLOCKING,
    'forge_problem': BLOCKING, 'environment': BLOCKING, 'observation_unavailable': BLOCKING, 'legacy_reconciliation': BLOCKING,
    'dependency': BLOCKING,
    'recovery_pending': TRANSITIONAL, 'cleanup_pending': TRANSITIONAL, 'result_pending': TRANSITIONAL,
    'provider_unavailable': BLOCKING, 'capacity': BLOCKING, 'retry_delay': BLOCKING,
    'verification_human': BLOCKING, 'verification_ci': BLOCKING, 'approval_required': BLOCKING,
    'changes_requested': BLOCKING, 'merge_checks': BLOCKING, 'merge_conflict': BLOCKING,
}


def transitional(code) -> bool:
    """Whether this gate is the current stage settling rather than a hold."""
    return GATE_PRESENTATION.get(code) == TRANSITIONAL


# --- Jobs and artifacts ----------------------------------------------------

JOB_ROLES = ('build', 'integration', 'review', 'audit', 'recovery', 'merge')

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
    'worktree_bundle': 'durable',
    'input_manifest': 'durable',
    'result': 'durable',
    'summary': 'durable',
}
