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
