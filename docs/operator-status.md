# Operator status across public views

SQLite Activity rows, issue list/detail responses, and `forge repo activity`,
`forge repo issues`, and `forge repo issue` include the same `operator_status`:

```json
{
  "code": "waiting",
  "label": "Waiting",
  "reason_code": "result_pending",
  "reason": "Checking the completed worker result"
}
```

`code` is the broad operator state. Its values are `backlog`, `queued`, `working`,
`attention`, `waiting`, `blocked`, `completed`, and `dropped`. `label` is the badge
wording. `reason_code` identifies the selected lifecycle gate, current stage,
or completion reason; `reason` is its separate explanation. Untracked issues
have a null reason code. `stage_code` remains the lifecycle stage, even while a
gate blocks it. The main label does not assert that no process is running: a
worker can remain visible while an ownership gate shows **Blocked**. Its job
status and cleanup are separate facts.

The server builds this object once in `factory/status_projection.py`; Activity,
list and detail reuse it. CLI reads return the scoped API response unchanged.
The UI renders the supplied label and reason directly, including the Activity
line and detail pipeline. Its single compatibility helper handles responses
from older servers that omit the object. Legacy state, summaries, job phase,
and observation age cannot override a supplied status.

Completion elsewhere and a Forge merge both show **Completed**, with different
reason codes. Reopening an issue removes its old completion from the current
projection. Cleanup and result validation show **Waiting**, with separate
reasons. Releasing a hold preserves any independently scoped remaining gate.
None of these presentation fields changes dispatch, authorization, or gates.

`tests/fixtures/operator-status.json` is the shared acceptance matrix. Python
creates its cases with typed lifecycle commands and checks public projections,
SQL filters, scoped client reads and CLI output. The UI test uses the same
fixture for Activity cards/line, issue cards/detail labels and pipeline reasons,
including contradictory legacy fields that must not override canonical values.

## Reloading after a changed issue or pull request

A rejected stale decision remains visible as a rejected request; it does not add
an execution hold. Refresh the issue and submit a new decision with its current
revision. Forge also schedules a fresh observation after rejection.

A genuine external-change hold offers **Reload and continue**. Forge rereads the
issue and PR. An exact, restored candidate can resume after observation. For an
open PR with changed commits, the operator action invalidates the old candidate
and its reviews, retains only the exact owned PR as rebuild input, and repeats
Review and Audit. Other holds and the failure budget remain intact.

A draft must be marked ready, a closed PR reopened, or a changed target branch
restored on GitHub before continuing. A changed PR already merged needs merge
reconciliation; this action cannot certify different merged commits. Migration
035 removes only historical rejection-created holds with a `request_id`, leaving
actual external-change holds and their evidence untouched.
