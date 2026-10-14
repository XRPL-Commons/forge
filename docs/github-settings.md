# GitHub setup and repository access

> Guided onboarding (`/onboarding`) creates the four Apps through GitHub's manifest
> flow, stores their keys privately and writes this configuration for you. This page
> is the reference for what it produces, for the `/settings` pages, and for CLI-only
> installations that configure GitHub by hand.

Forge separates three decisions: GitHub grants repository access, the settings
sync discovers it, and `factory.json` records which repositories the operator
wants to automate. Discovery never enables a repository automatically.

The `/settings` page provides App connection forms, a repository access matrix,
activation controls, sync timing, and a JSON editor. UI edits and agent edits use
the same configuration file. Saves validate the complete configuration and reject
stale revisions rather than overwriting another editor's changes.

## Start disabled

The settings component requires Python 3.10+, OpenSSL, and outbound HTTPS to
`api.github.com`. It uses the Python standard library. No administrator token,
OAuth client secret, webhook endpoint, or additional Python package is needed.

A new installation has no connections or enabled repositories. The default sync interval
is five minutes; access observations expire after fifteen minutes. A settings GET
reads local data only. Refresh requests run in a background thread. Each installation sync
has a 90-second deadline, with up to 15 seconds for its final in-flight HTTP call;
large or inconsistent paginated responses fail rather than becoming empty lists.

The settings UI is the operator UI's `/settings` page. `bin/forge setup` installs the
UI and the loopback status API it reads, with `FORGE_ROOT` set to the runtime root
that holds `factory.json` and `state/`; see [UI deployment](ui-deployment.md). Keep
the UI on a trusted private network. Mutating requests require configured
[operator sign-in](operator-sign-in.md), a signed-in operator, same-origin JSON and
that session's CSRF token; without sign-in the API offers no control.

## Create and install role Apps

In the owning account or organization's GitHub settings, open **Developer settings
→ GitHub Apps → New GitHub App**. Create four distinct Apps with names unique to
your installation. Each role has its own identity; multiple installation tokens
for the same App would not create distinct role identities.

- Use the owning account URL as the homepage.
- Leave user authorization, device flow, callback and setup URLs unset.
- Disable webhook Active; the first version uses polling.
- For one account, choose Only on this account. To manage repositories across
  accounts, allow installation on those accounts and configure each installation ID.
  Open-source adopters create their own Apps; they do not need maintainer credentials.
- Leave organization and account permissions unset.

Grant these repository permissions (Metadata read access is implicit):

| Permission | Builder | Reviewer | Auditor | Controller |
| --- | --- | --- | --- | --- |
| Contents | Read & write | Read | Read | Read & write |
| Issues | Read & write | Read | Read | Read & write |
| Pull requests | Read & write | Read & write | Read & write | Read & write |
| Checks | None | Read & write | Read & write | Read & write |
| Administration | None | None | None | Read |

These are minimum role capabilities for the factory. Extra permissions
are not required by discovery. Editing GitHub Actions workflow files additionally
requires Builder Workflows write permission and an explicit repository policy
allowing that work. Administration write permission is not requested; repository
protections remain a human setup responsibility. The controller reads them before dispatch. Settings access readiness does not certify branch
protections or authorize a merge.

Install every App on the same pilot repository using **Only select repositories**.
Access must be granted separately for each App. Record the numeric App ID from
its settings and the installation ID from its installation settings URL. Generate
one private key per App. Do not put private keys, installation tokens, or customer
repository configuration in version control or issue comments.

## Private key references

Onboarding stores each App's private key in the installation's credential store
(`<root>/credentials/`, mode 0700, each key 0600) under the reference
`FORGE_SETUP_<ROLE>_KEY`. Configuration holds only that reference name. Forge reads
App keys from the credential store only, never from the process environment.
Runners do not inherit App private keys or the controller's environment. Reconnecting
an App in Settings replaces its stored key; the next sync uses it.

In Settings, enter App ID, installation ID, and the corresponding credential
reference for each role. Save connections, then click **Refresh access**. The
sync authenticates each App, verifies its installation identity and grants, and
creates a metadata-only installation token to list accessible repositories. Tokens
and JWTs exist only in memory; error responses omit GitHub response bodies and
private key paths. No repository, App installation, label, or protection is changed.

An example connection entry, using placeholder identifiers:

```json
"build": {
  "app_id": 123456,
  "installation_id": 234567,
  "private_key_env": "FORGE_SETUP_BUILD_KEY"
}
```

## Enable, reconcile, dispatch

Each discovered repository initially shows Enable. All four roles must have a
fresh, successful observation, the required grants, and access to the same numeric
repository identity. Archived or disabled repositories are ineligible. The Enable
dialog explains that eligible issues may be processed after execution setup and
initial reconciliation. Enabling stores the repository ID, current name, and
`enabled: true` in `factory.json`.

Repository renames or transfers require updating the configured name after checking
the identity and access. A newly created repository with the old name must not
inherit activation from a different numeric ID.

The supervisor's snapshot and capacity/assignment gates consult this access state.
An enabled repository with missing, expired, suspended, or unverified access cannot
start new work. Failed syncs preserve intent and the last known inventory but do
not authorize dispatch from stale facts. Existing issue history is retained.
`factory.json` is authoritative for repository activation. Do not grant runners
access to the App keys; they receive repository-scoped role tokens only. See
[implementation status](implementation.md).

## Sync without the dashboard

```sh
bin/forge settings sync --root ./local
bin/forge settings watch --root ./local
```

`watch` syncs at startup and on the configured interval. The `forge-status` API
also runs this loop. A shared file lock prevents concurrent syncs from duplicating API work.
The one-shot `sync` command exits nonzero when configured access cannot be verified;
exit code 2 means another sync already owns the lock.
A crash leaves an interrupted indicator; restarting or clicking Refresh retries
the observation. Configuration changes invalidate observations for affected App
connections immediately. Failed refreshes never remove configured repositories.

## References

- [Register a GitHub App](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app)
- [Install your GitHub App](https://docs.github.com/en/apps/using-github-apps/installing-your-own-github-app)
- [Manage private keys](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/managing-private-keys-for-github-apps)
- [List installation repositories](https://docs.github.com/en/rest/apps/installations#list-repositories-accessible-to-the-app-installation)

## Durable factory integration

The new controller and resettable runners are implemented in `forge/factory`; see
[factory runtime](factory-runtime.md) for image/native-sandbox provisioning, the
three-failure budget and approval enforcement. Controller
App permissions now also include **Administration: read** for branch-protection
verification. This does not grant administration writes.

For repositories across accounts, register Apps for installation on the relevant
accounts and install each role App on both accounts. Enter comma-separated
installation IDs in Settings, or use `installation_ids: [101, 202]` instead of
`installation_id` in a connection. Forge maps repository IDs to their installation
before minting a scoped token. Changing that mapping requires a fresh sync. A failed
installation observation conservatively pauses the role until access is known.

`runtime.json` starts dispatch and merge disabled and is editable in Settings.
Authorized human logins and the pilot repository still need real operator values;
examples deliberately contain no private identities, keys or enabled repositories.
