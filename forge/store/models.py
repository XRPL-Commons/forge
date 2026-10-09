"""Typed command identities for the inactive SQLite lifecycle foundation."""
from dataclasses import dataclass
from enum import Enum


from .codes import UNIT_STATES, JOB_ROLES, GATES

# Compatibility enums derive from the closed registry; it is the only Python
# source for valid persisted lifecycle values.
Phase = Enum("Phase", {value.upper(): value for value in UNIT_STATES}, type=str)
Role = Enum("Role", {value.upper(): value for value in JOB_ROLES}, type=str)
GateCode = Enum("GateCode", {value.upper(): value for value in GATES}, type=str)


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


class RequirementsStale(TransitionError):
    code = 'requirements_stale'


def complete_requirements(requirements):
    return not (isinstance(requirements, dict) and
                (requirements.get('body_truncated') or requirements.get('body_unavailable')
                 or requirements.get('complete') is False or 'legacy_body_hash' in requirements))
