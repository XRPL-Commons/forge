# Fresh-host installation from your computer

`forge setup new` guides a fresh Ubuntu host through installation, private HTTPS,
operator sign-in, four GitHub role Apps, a coding account, repository checks and
explicit Start. It then follows one issue through Build, Review, Audit and a
verified merge. Each stage also has a noninteractive command.

`installed` means the host passed qualification. `factory_active` means you
explicitly started processing. Only `first_merge_complete` confirms the final
milestone.

## Install the client

The client needs Python 3.10+, Git, OpenSSH and authenticated GitHub CLI (`gh`)
on macOS or Linux. GitHub administration uses that local account. Python must
include `venv` support. Install the client with [uv](https://docs.astral.sh/uv/):

```sh
uv tool install 'git+https://github.com/XRPL-Commons/forge.git'
forge setup new
```

The repository is currently private, so installing from it requires your local
GitHub access. Do not put a GitHub token into a curl command or shell history.

## Guided installation

```sh
forge setup new
```

The wizard asks for the SSH account and address, factory name, host trust, and
installation confirmation. Use a dedicated Ubuntu 24.04 VM with AppArmor enabled
and systemd. The SSH account must be root or have noninteractive sudo; configure
the VM to accept your local SSH key through your VM provider. The remote image
must include `/usr/bin/python3`. No remote terminal or manual file transfer is
needed: the CLI runs the host operations and transfers the required files.

For a new host, compare the displayed ED25519 fingerprint with the VM provider
console or another trusted source before accepting it. The client pins that key
for subsequent requests. If SSH already knows the host, the existing trust
configuration can be used. Forwarding is disabled in both cases.

The plan shows the host, source commit, and effects:

- Install Ubuntu's Docker Engine, Buildx and Compose plugins, Git, Python, and AppArmor
  tools using the host's package repositories.
- Create the dedicated non-root `forge` account, Docker group membership, and
  lingering user service manager.
- Fetch the chosen Forge commit locally, transfer its commit and tree as a Git
  object pack, and check it out at `/home/forge/forge`. Local Git configuration,
  credentials, and older source history are excluded. The VM does not need a
  GitHub administrator token to obtain the private source.
- Install the matching runner AppArmor policy.
- Qualify the private SQLite runtime and initialize a selected empty store before
  starting services.
- Invoke `bin/forge setup install`, which builds the UI and worker
  image, runs runner qualification, and starts loopback services.

The runtime is `/home/forge/.local/share/forge`. Host installation does not start
issue processing. An existing `forge` account or source without matching installer
state is a conflict; this command does not adopt it or wipe the VM.

After host installation, the wizard continues through the remaining stages below.
Closing it preserves progress. Use `forge setup new --ssh USER@HOST` to resume
from the host stage, or `forge setup continue --ssh USER@HOST` after sign-in setup.

## Private network and operator sign-in

For a human operator, continue sign-in with:

```sh
forge setup sign-in --ssh USER@HOST
```

The wizard explains which steps happen in GitHub and which the CLI performs.
It opens the registration page, prints the exact App name and callback fields,
asks for the public client ID, and explains how to generate and copy the client
secret before showing a hidden prompt. It also explains passkey verification,
which credential to use, where the secret is stored, and how to resume. You do
not need an agent to fill the GitHub form. GitHub registration and passkey checks
remain human browser steps; the CLI does not create the App automatically.

The command uses your saved factory settings, so it skips registration when the
client ID is already known. Terminal output is readable text. `--json` retains
the noninteractive protocol, and `identity` remains an alias-compatible command.

These stages also have noninteractive commands:

```sh
forge setup network --ssh USER@HOST --json
forge setup network --ssh USER@HOST --apply --wait 60 --json
forge setup identity --ssh USER@HOST --operator GITHUB_LOGIN --json
forge setup identity --ssh USER@HOST --client-id PUBLIC_CLIENT_ID --json
forge setup identity --ssh USER@HOST --apply
```

Network setup installs Tailscale from its signed Ubuntu repository and returns
a browser link for joining your tailnet. Approve the VM, then run `forge setup
network --ssh USER@HOST --json` again. `network_ready` confirms its private HTTPS
name; the UI still listens on loopback. Hostname defaults to the factory name
and is retained across retries. Use `--hostname` on the first network operation
to choose another name.

Identity setup resolves operator GitHub logins locally and returns the exact
registration fields for a dedicated GitHub OAuth App. Create the App through
the returned browser link, then supply its public client ID. The client remembers
the operator, client ID, and verified network origin. It prompts privately for
the identity App secret when applying. Agents can instead use `--apply
--secret-stdin --json` with a private secret source; never put the secret in shell
arguments, agent messages, or a setup journal.

The CLI invokes the existing sign-in bootstrap as the installed `forge` account.
It configures the authorized operator IDs, reconciles loopback services, enables
private Tailscale HTTPS, and verifies HTTPS from both computers. The secret is
sent only through encrypted SSH stdin and stored in Forge's private runtime.
Local GitHub administrative credentials are not transferred.

| Status | Meaning and next action |
| --- | --- |
| `network_required` | Review and apply private network setup. |
| `network_installing` | The durable network job is running; poll the same command. |
| `tailnet_authorization_required` | Open the returned link, approve the VM, then poll. |
| `tailnet_approval_required` | Approve the VM in the returned Tailscale admin page, then poll. |
| `network_interrupted` | Correct the reported prerequisite and retry with `--apply`. |
| `network_ready` | The VM has a private network address; continue to identity setup. |
| `identity_app_required` | Register the identity App using the returned fields. |
| `identity_secret_required` | Supply the App secret through the private input method. |
| `planned` | Review the identity plan and apply it. |
| `sign_in_required` | HTTPS is verified; open the returned login page and sign in. |

After an interrupted identity apply, use `forge setup identity --ssh USER@HOST
--resume --json`. Existing identity settings, sessions, and grants are preserved.
For humans, `forge setup sign-in --ssh USER@HOST --resume` prints the result and
next step in plain language. If settings were saved before an HTTPS check failed,
the CLI says that the secret is saved and gives this exact recovery command.
Different operator or App settings produce a conflict rather than replacing
them. A stopped network installer can be repaired explicitly with `forge setup
network --ssh USER@HOST --apply --upgrade-installer`; the source commit and setup
identity remain pinned.

`sign_in_required` confirms configuration, not a completed browser sign-in or a
working factory. Continue with the role Apps and factory checks below.

## Role Apps, coding account and first repository

```sh
forge setup continue --ssh USER@HOST
```

The wizard resumes completed stages. Sign in to Forge with the GitHub account
used by your local `gh`. Choose the organization or user that owns the four Apps.
For each role, the CLI opens a short-lived Forge page; its button submits the
role-specific manifest directly to GitHub. Approve registration and install the
App on the chosen repository. Forge exchanges GitHub's one-use code for that
App's credentials. It never receives your personal or administrator token.

The handoff is bound to the signed-in operator, immutable GitHub owner ID, role,
server identity and setup generation, and expires after ten minutes. Resume the
same command if a handoff expires. Passkey and organization approvals remain
browser actions performed by the operator.

After each App, choose **check** in the CLI. It refreshes GitHub access and stays
on that role until registration and installation access are verified. Choose
**reopen browser** to return to the same step: a registered App opens its existing
installation link; an expired registration handoff is renewed. **Save and exit**
keeps progress for `forge setup continue --ssh USER@HOST`. Verified roles are
skipped on resume.

If GitHub passkey verification leaves you on a blank App registration form,
complete verification, then choose **reopen browser** in the CLI and use the
Forge page's **Continue on GitHub** button again. Do not fill in that blank form
manually. The CLI supplies the role manifest and handles the remaining setup;
you perform the browser approvals and select the repository installation.

Next, choose a coding account. For browser login, the CLI opens an interactive
provider login on the host over pinned SSH and gives you its browser instructions.
API keys and tokens use a hidden prompt or private stdin. Account credentials stay
in the server's private credential files, outside the local setup journal.

For the first repository, the CLI prints its preparation plan. Applying it uses
local GitHub access to grant App access or add the required review baseline;
Forge independently verifies readiness. Existing protections are preserved.
The repository must support GitHub rulesets. If GitHub explicitly reports that
the current plan does not support them, setup returns `github_plan_required`
before making changes. For a private repository, enable a GitHub plan that
supports private-repository rulesets and rerun the repository command, or defer
setup. Forge does not change repository visibility or billing. Other permission
errors are not treated as evidence of a plan restriction.
The wizard then runs isolation checks and small real model probes. **Start** is a
separate confirmation that enables processing for the selected repository.

The same stages are available to agents:

```sh
forge setup inspect --ssh USER@HOST --json
forge setup apps --ssh USER@HOST --owner OWNER --role build --apply --json
# Repeat the returned browser handoff for review, audit and controller.
forge setup apps --ssh USER@HOST --apply --json
forge setup account --ssh USER@HOST --provider codex --kind api_key --secret-stdin --apply --json
forge setup repository --ssh USER@HOST --repository OWNER/REPO --json
forge setup repository --ssh USER@HOST --repository OWNER/REPO --apply --json
forge setup check --ssh USER@HOST --apply --wait 60 --json
forge setup start --ssh USER@HOST --apply --wait 60 --json
```

Feed account credentials from a private secret source; never paste them into
commands or agent messages. JSON browser-account mode returns a terminal handoff
instead of attempting an interactive login. `apps` without a role chooses the
next missing App; after all four exist, `--apply` refreshes their installations.

| Status | Meaning and next action |
| --- | --- |
| `setup_required` | Inspect role, account and repository checks; finish the missing stage. |
| `app_registration_required` | Open the returned operator-bound link and approve the GitHub registration. |
| `app_registered` | Complete the App installation through its returned GitHub URL. |
| `account_login_required` | Complete terminal login or supply a credential through private stdin. |
| `repository_ready` | Repository preparation passed; run verification, then Start. |
| `write_unconfirmed` | A GitHub write may have succeeded; inspect and verify it before retrying. |
| `checking` | A durable setup operation is running; inspect again. |
| `factory_active` | Start completed. Choose the first merge issue. |

An uncertain GitHub write is verified on resume before another write is attempted.
Setup identity, source, owner and first repository stay bound to the saved run.
After Start, add further repositories using the normal scoped
[repository commands](agent-connections.md#repository-operations).

## First merge and measurement

Choose an existing open issue with clear acceptance criteria. The wizard asks
before adding the configured ready label. It then observes normal processing;
operator decisions, human verification and retries remain available in Forge.

```sh
forge setup pilot --ssh USER@HOST --repository OWNER/REPO --issue 123 --apply --wait 60 --json
forge setup pilot --ssh USER@HOST --wait 60 --json
```

The first command binds the issue and marks it ready. Subsequent calls reuse it.
`first_merge_pending` reports current status and attempt counts. Closing the
terminal, changing workers or retrying the issue does not restart the timer.
The command does not bypass an approval or automatically reset a failure budget.

`first_merge_complete` requires successful native Build, Review and Audit jobs,
a confirmed normal merge effect, and an independent local GitHub read matching
the exact candidate head and merge commit. An issue closed elsewhere or an
adopted existing pull request does not satisfy this milestone.

The receipt includes `setup_to_first_merge_seconds`, measured from the recorded
setup start to GitHub's merge timestamp. No remote telemetry is sent.

## Noninteractive operations

Discover command schemas with `forge --json commands`. JSON mode never prompts
or opens a browser. Host-key discovery does not trust the returned key:

```sh
forge setup host-key --ssh root@HOST --json
forge setup new --ssh root@HOST --name Anvil --host-key SHA256:VERIFIED_FINGERPRINT --json
forge setup apply --ssh root@HOST --json
forge setup status --ssh root@HOST --json
forge setup resume --ssh root@HOST --wait 60 --json
```

`setup new --json` and `setup plan` create a plan. Apply requires a saved plan.
Use `--revision FULL_COMMIT_SHA` on the plan to select an exact source version;
otherwise the client resolves main once and saves that commit. Resume keeps it.

| Status | Meaning and next action |
| --- | --- |
| `planned` | The VM passed initial inspection; review the plan and apply it. |
| `installing` | A systemd job owns installation; poll status or resume watching. |
| `interrupted` | Installation stopped; inspect the named stage and use resume to retry. |
| `installed` | Host qualification and initial service checks passed; full factory onboarding remains required. |

Results include the setup ID, destination, pinned revision, host capacity, current
step, completed steps, error code, and next action. Raw subprocess diagnostics
and credential values are not returned. Host packages use the configured Ubuntu
package repositories; pinning Forge's commit is not a claim that every OS package
or network response is reproducible byte for byte.

## Interruptions and clean-install tests

The remote systemd job survives a closed terminal or lost SSH connection. Local
state is mode 0600 under `~/.local/state/forge-client`. Installer state and logs
are private under `/var/lib/forge-install` on the VM. Retrying reuses the setup
identity, skips completed stages, and reruns an interrupted stage idempotently.
Status does not resume or start an installation. The original client installer
payload is pinned too; use that client version to resume an interrupted run.

When an installer defect is fixed, `forge setup resume --ssh USER@HOST
--upgrade-installer` explicitly replaces the stopped installer implementation.
It preserves the setup ID, pinned server source, start time and completed
stages. Changed package prerequisites are reconciled again. It cannot replace a running job and does not upgrade the Forge server
source. Ordinary resume never changes the installer implementation silently.

After deliberately rebuilding the disposable VM, verify its new host identity
and start a new measurement run:

```sh
forge setup new --ssh root@HOST --name Anvil --new-run
```

`--new-run` archives the previous local run only after confirming that the target
is fresh. It never wipes a host or overrides an existing remote installation.
The existing Forge production server is outside this test workflow.

`setup_elapsed_seconds` remains a progress measurement; use the verified first-merge
receipt above for the completed run.

## Qualify signed publication on an existing host

The host must run the same release as the client. Connect the intended target
repository first, then choose an **existing private scratch repository** in the
same owner and Controller installation. The scratch repository must have an
existing branch. This command does not create repositories, change App
installations or permissions, or start automation.

```sh
forge --json setup qualify-publication --ssh anvil \
  --repo OWNER/TARGET --probe-repo OWNER/SCRATCH
```

The defaults select service user `forge`, source `/home/forge/forge` and runtime
`/home/forge/.local/share/forge`. For an existing installation with a different
layout, select all three explicitly. For example:

```sh
forge --json setup qualify-publication --ssh forge \
  --service-user operator --source /srv/forge/source \
  --root /var/lib/forge \
  --repo OWNER/TARGET --probe-repo OWNER/SCRATCH
```

The CLI uses the same account-switching transport and path validation as
`forge migrate upgrade`. It needs the existing account's SSH access, or root or
noninteractive sudo authority to switch to that account. The selected checkout
must already contain `forge.publication_qualification`; the command returns
`qualification_module_not_installed` otherwise. It does not install, update or
copy the module. These examples do not establish live acceptance on an older
production release; deploy the reviewed release during its authorized upgrade
window before running its qualification command.

The default is a read-only plan. Add `--apply` to create a small text-file commit
and update the scratch branch. Use `--target-branch` and `--probe-ref` to select
branches explicitly; otherwise each repository's default branch is used.
Personal GitHub credentials stay local. The host uses separate target-read and
scratch-write App tokens and revokes them afterward.

The probe first uses the Git Data API without a custom author or committer.
Only a confirmed unsigned commit permits the GraphQL fallback. The host records
the operation before each write and checks the resulting commit, tree, author,
signature and exact ref. If the target requires signatures, the same active
organization signing rules must apply to the scratch ref, with no Controller
bypass; GitHub's rule-suite evidence must show an enforced pass for this exact
update. Classic and repository-local signing requirements currently return an
explicit unsupported result. A target without a signing requirement receives
signed-API qualification without a claim about rule enforcement.

Keep the returned `receipt_id`. A delayed rule suite or lost API response can
be checked again without repeating writes:

```sh
forge --json setup qualify-publication --ssh anvil \
  --repo OWNER/TARGET --probe-repo OWNER/SCRATCH --receipt RECEIPT_ID --apply
```

An uncertain unattached REST commit cannot be inferred from the branch and
remains unresolved. The command does not retry that write or import a
caller-supplied success flag. Qualification is bound to the current Controller
App, installation, target branch and signing rules; changed context requires
qualification again.

Workflow-file permissions are a separate explicit probe after qualification:

```sh
forge --json setup qualify-publication --ssh anvil \
  --repo OWNER/TARGET --probe-repo OWNER/SCRATCH --receipt RECEIPT_ID \
  --check-workflow --apply
```

This attempts a workflow-file change with Contents-only permission. A positive
case runs only when the installation already grants Workflows write. It never
grants permission or dispatches a workflow. HTTP refusal is recorded without
claiming its permission cause. A missing Workflows grant does not invalidate
ordinary-file publication. Repeating the command reads the saved cases and
reconciles uncertain outcomes without repeating writes.

Finish and retain the probe receipts before deleting the scratch repository.
Scratch deletion remains a separately authorized operation. This qualification
does not replace the native boundary, every-role or real issue-to-merge canaries.
