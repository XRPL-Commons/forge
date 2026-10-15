"""The code registry Python reads: unit states, gate codes and close codes.

Wording lives in `status.describe()`; nothing compares prose. The schema seeds
the full registry (including unit transitions, job phases and event kinds) into
reference tables so the database rejects an unknown code, and
`tests/test_store_codes.py` checks that the codes here agree with it. Changing a
code needs a new migration. `legacy_reconciliation`, `retry_delay` and
`merge_checks` are no longer opened; they stay because stores keep resolved
imported gates that reference them.
"""
from __future__ import annotations

from dataclasses import dataclass

# --- Units -----------------------------------------------------------------

# A state names the phase, including time waiting for a worker. It never asserts
# that a worker is running; current jobs say that.
UNIT_STATES = ('building', 'publishing', 'reviewing', 'auditing',
               'merging', 'merged', 'closed')
TERMINAL_STATES = frozenset({'merged', 'closed'})


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
    'dissolved': CloseCode(None),
    # An imported delivery retired by migration 041; discovery admits its open
    # issue again as a new delivery.
    'import_superseded': CloseCode(None),
}

# --- Gates -----------------------------------------------------------------

@dataclass(frozen=True)
class GateClass:
    owner: str
    exit: str


GATE_CLASSES = {
    'transitional': GateClass('core', 'operation_confirmed'),
    'supervisor-remedy': GateClass('supervisor', 'remedy_verified'),
    'operator-choice': GateClass('operator', 'authorized_choice'),
    'external': GateClass('external', 'prerequisite_observed'),
}


@dataclass(frozen=True)
class Gate:
    kind: str
    release: str
    scopes: frozenset[str]
    priority: int
    public_status: str

    @property
    def candidate(self):
        return self.scopes == frozenset({'candidate'})

    @property
    def owner(self):
        return GATE_CLASSES[self.kind].owner


# Codes preserve exact release provenance and historical references. Their
# class owns progress; the explicit release authority cannot clear other holds.
GATES: dict[str, Gate] = {
    'operator_pause': Gate('operator-choice', 'operator', frozenset(('member', 'unit')), 2, 'backlog'),
    'operator_backlog': Gate('operator-choice', 'operator', frozenset(('member', 'unit')), 2, 'backlog'),
    'label_hold': Gate('external', 'observation', frozenset(('member', 'unit')), 2, 'blocked'),
    'membership_decision': Gate('operator-choice', 'operator', frozenset(('member', 'unit')), 2, 'attention'),
    'operator_sync': Gate('transitional', 'observation', frozenset(('member', 'unit')), 2, 'working'),
    'triage_sync': Gate('transitional', 'observation', frozenset(('member',)), 2, 'working'),
    'external_change': Gate('supervisor-remedy', 'observation', frozenset(('member', 'unit')), 2, 'attention'),
    'ownership_conflict': Gate('external', 'observation', frozenset(('member', 'unit')), 2, 'blocked'),
    'budget_exhausted': Gate('operator-choice', 'operator', frozenset(('unit',)), 3, 'attention'),
    'approach_required': Gate('supervisor-remedy', 'operator', frozenset(('member', 'unit')), 3, 'attention'),
    'decision_required': Gate('operator-choice', 'operator', frozenset(('job', 'member', 'unit')), 3, 'attention'),
    'forge_problem': Gate('supervisor-remedy', 'observation', frozenset(('candidate', 'job', 'member', 'unit')), 4, 'attention'),
    'repository_setup': Gate('external', 'observation', frozenset(('unit',)), 4, 'attention'),
    'environment': Gate('external', 'recheck', frozenset(('job', 'member', 'unit')), 4, 'attention'),
    'observation_unavailable': Gate('supervisor-remedy', 'observation', frozenset(('candidate', 'member', 'unit')), 4, 'attention'),
    'legacy_reconciliation': Gate('operator-choice', 'operator', frozenset(('member', 'unit')), 4, 'attention'),
    'dependency': Gate('external', 'observation', frozenset(('job', 'member', 'unit')), 5, 'blocked'),
    'recovery_pending': Gate('transitional', 'transition', frozenset(('candidate', 'job', 'member', 'unit')), 6, 'working'),
    'cleanup_pending': Gate('transitional', 'transition', frozenset(('candidate', 'job', 'member', 'unit')), 6, 'working'),
    'result_pending': Gate('transitional', 'transition', frozenset(('candidate', 'job', 'member', 'unit')), 6, 'working'),
    'provider_unavailable': Gate('external', 'time', frozenset(('unit',)), 6, 'waiting'),
    'retry_delay': Gate('supervisor-remedy', 'time', frozenset(('member', 'unit')), 6, 'waiting'),
    'verification_human': Gate('external', 'evidence', frozenset(('candidate',)), 7, 'attention'),
    'verification_ci': Gate('external', 'evidence', frozenset(('candidate',)), 7, 'waiting'),
    'approval_required': Gate('external', 'evidence', frozenset(('candidate',)), 7, 'attention'),
    'merge_checks': Gate('external', 'observation', frozenset(('candidate',)), 7, 'waiting'),
}


def transitional(code) -> bool:
    gate = GATES.get(code)
    return gate is not None and gate.kind == 'transitional'


# --- Jobs and artifacts ----------------------------------------------------

JOB_ROLES = ('build', 'integration', 'review', 'audit', 'recovery', 'steward', 'merge')

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
    'steward_dossier': 'durable',
    'steward_decision': 'durable',
}
