# Builds that need a decision

When code cannot meet an issue's requirements, the builder returns `needs_decision`
instead of opening a pull request of documents. Its `decision` contains a question,
two to six options, a recommendation and a short evidence summary. Other outcomes
use `decision: null`; results from older runner images remain readable.

The supervisor verifies that the assignment and issue are current, then places the
issue on hold. The attempt is cleaned up without a charged failure, recovery job,
review or audit. The question stays visible in the issue panel across restarts.
Discuss the options or choose **Retry attempt** and record the answer. Forge saves
the answer in history and passes it as the next build's retry plan. An answer does
not waive review, audit or merge checks.

Raw audit output, registry metadata and logs must not be committed unless the issue
explicitly requests those files. Summarize relevant evidence in the worker result,
PR description or issue discussion.

## Continuing existing drafts

Before a build without a candidate, Forge finds open PRs from this installation's
build App on `forge/issue-<number>-*` branches in the same repository. It chooses the
most recently updated PR, breaking timestamp ties by the highest PR number. The
`draft_resumed` history event records that PR and the superseded PR numbers. Forge
leaves duplicate PRs open for an operator to review.

The branch is resumed, but a draft is not an accepted candidate. The builder must
publish a ready PR with the current assignment's submission marker. The existing
`candidate()` checks then verify author, repository, branch, target and exact head
before any review starts.
