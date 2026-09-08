# Worker admission and host capacity

The supervisor admits at most three configured fleet sessions, with one slot per
role: builder, reviewer and auditor. One build remains allowed box-wide. The role
limits reserve room for downstream review and audit instead of allowing builders
to occupy every slot. This conservatively serializes legacy builders across repos.
Personal tmux sessions are not counted or controlled, but their RAM consumption is
included in host telemetry. Disabled-repository fleet survivors still count.

Before cloning, and again immediately before launching, the supervisor requires
at least 2 GiB MemAvailable after subtracting a 512 MiB launch allowance. It rejects
recent memory PSI above 1% some or 0.1% full, checking both 10-second and 60-second
averages. Missing/invalid tmux, configuration or memory data refuses admission.
The last decision is atomically saved to state/capacity.json before allowing a
launch; a failed save also refuses it. The dashboard displays this receipt, not a
claim of continuous monitoring. Capacity denials do not consume the launch budget.

This is a launch gate under the singleton supervisor, not a 6 GiB build-memory
reservation or protection against same-user workers bypassing the controller.
It cannot guarantee future memory use. Existing sessions are neither killed nor
preempted to meet limits. Idle fleet sessions retain their slots until the current
lifecycle permits teardown; queued work may wait. The structured executor must
reclaim completed attempts from trusted receipts and cgroup evidence. Never kill
a quiet pane merely to free capacity.

Repository declarations now pass through forge.config as data, with validated
absolute clone paths and recovery identities. No source/eval executes their text.
All declarations must validate before a poll admits work. A persisted cursor
rotates the starting repository between polls; this removes fixed alphabetical
preference without bypassing priority/dependency ordering inside a repository.
Corrupt cursor state fails closed and requires reconciliation.

CLI startup and nudges also check that the exact fleet pane foreground is Claude.
A shell or unknown process is parked, not given role instructions. This limits
accidental prompt-to-shell execution; terminal inspection remains a compatibility
check, not authenticated process or progress evidence.

Choose memory and concurrency settings from measurements on your own host. The public supervisor defaults to three fleet sessions and one per role; all limits remain configurable. This is an admission guard, not a guarantee against future memory exhaustion. Do not resize a host or increase concurrency solely to compensate for blocked human approvals or occupied idle role slots.
