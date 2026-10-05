"""Plain wording for lifecycle codes, in one place.

The supervisor, API, UI and CLI show `describe(code, **params)`; none of them
compares or parses this text. Each entry is a sentence that needs no parameters,
plus an optional richer sentence used when its parameters are supplied.
"""
from __future__ import annotations

from . import codes

WORDING: dict[str, tuple[str, str | None]] = {
    # Unit states
    'queued': ('Waiting to start', None),
    'building': ('Building', 'Building {parts}'),
    'integrating': ('Combining the parts into one change', None),
    'reviewing': ('In review', 'Reviewing pull request #{pull}'),
    'auditing': ('In audit', 'Auditing pull request #{pull}'),
    'merging': ('Ready to merge', 'Merging pull request #{pull}'),
    'merged': ('Merged', 'Merged in pull request #{pull}'),
    'closed': ('Closed without merging', None),
    'dissolved': ('Dissolved; its issues continue separately', None),
    # Close codes
    'github_completed': ('Closed on GitHub as completed', None),
    'github_not_planned': ('Closed on GitHub as not planned', None),
    'github_duplicate': ('Closed on GitHub as a duplicate', 'Closed on GitHub as a duplicate of #{duplicate_of}'),
    'operator_closed': ('Stopped by the operator', None),
    'combined': ('Combined into another unit', 'Combined into unit {into}'),
    # Gates
    'operator_pause': ('Paused by the operator', None),
    'operator_backlog': ('Moved to the backlog by the operator', None),
    'label_hold': ('On hold by a GitHub label; removing the label resumes it', 'On hold by the "{label}" label; removing it resumes the work'),
    'membership_decision': ('Waiting for a decision about which issues stay in this unit', None),
    'operator_sync': ("Applying the operator's decision on GitHub", None),
    'external_change': ('Paused after a change made outside Forge', None),
    'ownership_conflict': ('Someone else is working on this issue', '{login} is working on this issue'),
    'budget_exhausted': ('Stopped after three failed attempts; choose how to continue', None),
    'approach_required': ('The same failure keeps recurring; a new approach is needed', None),
    'decision_required': ('Waiting for an operator decision', 'Waiting for an operator decision: {question}'),
    'repository_setup': ('Waiting for repository setup; work resumes once it is ready', None),
    'environment': ('Waiting for the build environment to be repaired', None),
    'observation_unavailable': ('GitHub could not be read; waiting to confirm the current state', None),
    'legacy_reconciliation': ('Imported from the old state; needs an operator review', None),
    'dependency': ('Waiting for other issues to close', 'Waiting for {issues} to close'),
    'recovery_pending': ('Investigating a failed attempt', None),
    'cleanup_pending': ('Cleaning up a finished attempt', None),
    'provider_unavailable': ('Waiting for an available agent account', 'Waiting for an agent account until {until}'),
    'capacity': ('Waiting for a free worker', None),
    'retry_delay': ('Waiting before retrying', 'Retrying after {until}'),
    'verification_human': ('Waiting for a person to verify the change', None),
    'verification_ci': ('Waiting for CI to verify the change', None),
    'approval_required': ('Waiting for approval to merge', None),
    'changes_requested': ('Changes were requested on the pull request', None),
    'merge_checks': ('Waiting for required checks to pass', None),
    'merge_conflict': ('The change conflicts with the base branch; rebuilding', None),
}


def describe(code: str, **params) -> str:
    """The plain sentence for a code. Unknown codes are a programming error."""
    try:
        plain, detailed = WORDING[code]
    except KeyError:
        raise KeyError(f'No wording for code {code!r}') from None
    if detailed and params:
        try:
            return detailed.format_map(params)
        except (KeyError, IndexError, ValueError):
            pass
    return plain


def registered_codes() -> set[str]:
    """Every code that must have wording."""
    return set(codes.UNIT_STATES) | set(codes.CLOSE_CODES) | set(codes.GATES)
