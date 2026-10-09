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

## Waiting for prerequisite issues

An open prerequisite is an automatic dependency wait. A builder returns
`waiting_on_issues`, with `issues: [90]` and `evidence` quoting the part of the
assigned issue that names `#90`. These must be issue numbers in the same repository;
self references and pull requests are rejected. Other build outcomes use `issues: []`
and `evidence: null`. Older runner results remain readable.

Forge verifies the quote and reads the prerequisite issues from GitHub. It combines
worker discoveries with dependencies parsed from the issue description, releases
the worker, and shows **Waiting for prerequisite issues**. This consumes no failure
budget and creates no environment hold or operator question. When every prerequisite
closes, a build starts automatically and continues the existing branch or draft.
An unavailable GitHub response keeps admission blocked until it can be verified.

The issue panel offers **Start anyway…** when only worker-reported prerequisites
are blocking. It requires an operator reason, records the cleared issue numbers in
history, and passes the reason to the next build. It preserves the failure budget
and all normal admission, review and merge checks. Explicit dependencies in the
issue description still apply. **Edit dependencies on GitHub** opens the issue;
editing its description also invalidates discoveries based on the old wording.

Use `blocked_environment/dependency_access` for package registry, repository or
network access failures. Use `needs_decision` for an actual human choice.

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
