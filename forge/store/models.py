"""Typed command identities for the inactive SQLite lifecycle foundation."""
from dataclasses import dataclass
from enum import Enum


class Phase(str, Enum):
    QUEUED = "queued"
    BUILDING = "building"
    INTEGRATING = "integrating"
    REVIEWING = "reviewing"
    AUDITING = "auditing"
    MERGING = "merging"
    MERGED = "merged"
    CLOSED = "closed"
    DISSOLVED = "dissolved"


class Role(str, Enum):
    BUILD = "build"
    REVIEW = "review"
    AUDIT = "audit"


class GateCode(str, Enum):
    OPERATOR_PAUSE = "operator_pause"
    OPERATOR_BACKLOG = "operator_backlog"
    LABEL_HOLD = "label_hold"
    MEMBERSHIP_DECISION = "membership_decision"
    OPERATOR_SYNC = "operator_sync"
    EXTERNAL_CHANGE = "external_change"
    OWNERSHIP_CONFLICT = "ownership_conflict"
    BUDGET_EXHAUSTED = "budget_exhausted"
    APPROACH_REQUIRED = "approach_required"
    DECISION_REQUIRED = "decision_required"
    REPOSITORY_SETUP = "repository_setup"
    ENVIRONMENT = "environment"
    OBSERVATION_UNAVAILABLE = "observation_unavailable"
    DEPENDENCY = "dependency"
    RECOVERY_PENDING = "recovery_pending"
    CLEANUP_PENDING = "cleanup_pending"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    CAPACITY = "capacity"
    RETRY_DELAY = "retry_delay"
    VERIFICATION_HUMAN = "verification_human"
    VERIFICATION_CI = "verification_ci"
    APPROVAL_REQUIRED = "approval_required"
    CHANGES_REQUESTED = "changes_requested"
    MERGE_CHECKS = "merge_checks"
    MERGE_CONFLICT = "merge_conflict"
    LEGACY_RECONCILIATION = "legacy_reconciliation"


@dataclass(frozen=True)
class Command:
    request_id: str
    actor_id: str
    unit_id: str
    expected_version: int


@dataclass(frozen=True)
class Candidate:
    pull_number: int
    branch: str
    head_oid: str
    base_oid: str
    policy_hash: str
    requirements_hash: str
    input_manifest_hash: str

    def __post_init__(self):
        if type(self.pull_number) is not int or self.pull_number < 1:
            raise ValueError("candidate requires a positive pull number")
        for name in ("branch", "head_oid", "base_oid", "policy_hash", "requirements_hash", "input_manifest_hash"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"candidate {name} must be nonempty")


class TransitionError(ValueError):
    """A command does not satisfy the lifecycle's current preconditions."""


class VersionConflict(TransitionError):
    pass


class IdempotencyConflict(TransitionError):
    pass
