# Git dependency access

Workers fetch Git dependencies through Forge's read-only mirror. The host uses repository-scoped GitHub App credentials; workers receive no GitHub credential. Clone and fetch requests are recorded on the job, including requests served from the cache.

Before launch, Forge reads supported dependency manifests from the assignment's pinned Git trees. It resolves declared Git dependency revisions and their nested manifests on the host. The resulting declaration evidence and access policy are frozen with the assignment. Runtime requests cannot add declarations by editing a manifest in the workspace.

Access requires both the assignment policy and current GitHub access. Declared repositories and repositories in organizations with a configured Forge App installation are allowed by policy. A private repository still needs actual read access from an installed App. Project restrictions override these allowances. Removing an App installation or adding a restriction narrows active sessions; it does not erase objects a worker has already fetched.

## Restrict a project's dependencies

Open **Settings → Repositories → your repository → Git dependency restrictions**. Enter one `owner/repository` per line. Optionally append its numeric GitHub repository ID after a space so the restriction also follows a rename. Save the restrictions. The form preserves existing numeric IDs and checks the settings revision before saving.

An empty list removes the project's explicit restrictions. It does not grant App permissions or expand an already frozen assignment policy. This operator control does not expand the agent connection's merge-policy capability.

The operator HTTP API uses the existing signed-in session and action token:

```text
GET /api/repositories?repo_id=123
POST /api/repositories/mirror
{"repo_id":123,"revision":"CURRENT_SETTINGS_REVISION","denied_repositories":[{"canonical_name":"acme/sensitive","repository_id":456}]}
```

The host administration CLI exposes the same revision-checked operation. This is the `bin/forge` command shipped with the server checkout:

```sh
bin/forge settings mirror --root /srv/forge --repo-id 123
bin/forge settings mirror --root /srv/forge --repo-id 123 \
  --revision CURRENT_SETTINGS_REVISION \
  --denied-json '[{"canonical_name":"acme/sensitive","repository_id":456}]'
```

Omitting `--denied-json` reads the current repository view and revision. Writing requires the revision from that read. The Settings UI is the remote operator path; a separate agent capability for changing dependency restrictions is not introduced.

## Inspect a refusal

Select a job on the issue page and expand **Git dependency fetches**. The view shows the latest 20 receipts and 20 typed refusals, with total counts. Complete records remain associated with the job in SQLite. Each refusal identifies the requested repository, its numeric identity when resolved, and the reason, such as `project_denied`, `installation_access_missing`, or `revision_unavailable`.

Recovery receives these structured facts from the failed job. It also receives the failed assignment's frozen source and notes, or the original pinned issue snapshots when older jobs have no frozen notes. Closing or editing an issue does not prevent diagnosis. A refusal is evidence for an operator decision; reading it does not retry a job or grant access.

Publication limitations appear alongside this evidence. A durable publication intent takes precedence over older pre-intent claims, so a confirmed publication does not keep showing a stale refusal.
