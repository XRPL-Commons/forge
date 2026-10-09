# OpenCode and Muse Code qualification

Checked on 2026-10-09 for [#5](https://github.com/XRPL-Commons/forge/issues/5)
and [#98](https://github.com/XRPL-Commons/forge/issues/98). Both issues remain open.
Neither provider is installed, selectable, or accepted by Forge's runtime
configuration. This checkpoint establishes distribution and offline CLI contracts;
it does not qualify model execution or native tool isolation.

## Reproducing the offline spike

Exact versions, official artifact URLs and checksums live in
[`deploy/provider_qualification/pins.json`](../deploy/provider_qualification/pins.json).
Download the two artifacts for the runner's architecture from those URLs. Do not
run the upstream floating installers. Then run, for example:

```sh
python3 deploy/qualify_optional_harnesses.py \
  --platform linux-arm64 \
  --opencode-archive /tmp/opencode-2.0.26-arm64.tgz \
  --muse-binary /tmp/muse-1.4.4-R5419.1-arm64 \
  --runner-image forge-runner-full:32fd66b122c170f7
```

The script checks both downloaded artifacts before executing either. It uses an
already installed runner image and a disposable non-root container with no network,
no credentials, a read-only root, and a temporary home. It verifies version/help
surfaces, an OpenCode synthetic credential round trip, Muse's embedded schema
fingerprint, and one Muse built-in `echo` run. The container is removed on exit,
including timeout. This is separate from `forge setup check` and cannot mark an
account ready.

Both Linux x64 and ARM64 artifact checksums were verified. The offline commands
were executed on the local ARM64 runner; the exact image ID and results are in
[`observed.json`](../deploy/provider_qualification/observed.json). Production x64 native tool behavior has
not been tested. The synthetic Muse wire transcript is retained in
[`tests/fixtures/harnesses/muse-1.4.4-R5419.1-echo.jsonl`](../tests/fixtures/harnesses/muse-1.4.4-R5419.1-echo.jsonl).
It is protocol evidence only: no model or repository tool ran.

## OpenCode 2.0.26

The current distribution is `@opencode/cli`, with platform packages including
`@opencode/cli-linux-x64` and `@opencode/cli-linux-arm64`. The tested headless
surface is `opencode run --standalone --format json --model …`. Standalone mode
owns a private server rather than joining a persistent shared one. Login remains
provider-specific. [Official CLI commands](https://opencode.ai/v2/docs/cli/commands/)

V2 stores accounts in `opencode.db`, so copying legacy `auth.json` would lose
updates. The pinned CLI provides `auth export [integration] --standalone` and
`auth import [file] --standalone` (stdin when no file is given). A synthetic API-key
entry survived that round trip offline. A future adapter can export only the
selected integration into Forge's private JSON credential lifecycle, import it
into each fresh job database, and publish refreshed exports through the existing
origin-digest check. Actual OAuth refresh, expired-login and reconnect races remain
untested. [Official provider storage documentation](https://opencode.ai/v2/docs/cli/providers/),
[pinned import implementation](https://github.com/anomalyco/opencode/blob/v2.0.26/packages/cli/src/commands/handlers/auth/import.ts)

**Blocking boundary:** the v2.0.26 local environment binds the host filesystem and
native process spawner directly. Shell creation supplies the session environment,
or the server's process environment when none is registered. A standalone process
containing a provider secret therefore has no demonstrated native isolation from
its repository tools. Permission approval rules do not establish an OS boundary.
A shell wrapper alone would also leave native file tools and process access to
qualify. No supported native policy meeting Forge's complete contract was found.
[Local environment source](https://github.com/anomalyco/opencode/blob/v2.0.26/packages/core/src/environment/local.ts),
[shell source](https://github.com/anomalyco/opencode/blob/v2.0.26/packages/core/src/shell.ts)

## Muse Code 1.4.4-R5419.1

The official launcher resolves a public channel manifest and a versioned release
manifest containing platform SHA256 hashes. Forge's spike downloads the pinned
artifact directly; it neither redistributes the executable nor runs the updater.
The embedded MSP schema fingerprint matches the release manifest.
[Official installer](https://dev.meta.ai/install.sh),
[official launcher](https://api.meta.ai/muse-launcher.sh),
[release manifest](https://lookaside.facebook.com/lookaside/muse/download/?channel=muse&version=1.4.4-R5419.1&file=manifest.json)

The tested binary exposes `exec --json --prompt-file --output-schema`, a model-step
limit, `--approval-mode never`, and `--user-input-auto-resolve`. The offline echo
emits schema-version-1 envelopes; completion is a `run.terminal.completed` payload,
not a Claude-style result object. The observed terminal carries its final text in
`payload.text`. Real structured Meta output and availability failures still need
an authenticated fixture. [Official automation documentation](https://dev.meta.ai/docs/muse-code/extending)

API authentication accepts `META_API_KEY` or `muse auth set --api-key-stdin`.
The launcher uses `~/.config/muse/auth.json` (or its documented environment
overrides); the actual stored subscription credential and refresh behavior were
not inspected because no account was connected.
[Official authentication documentation](https://dev.meta.ai/docs/muse-code/auth)

**Blocking boundary:** the documented native sandbox keeps `.git` read-only and
makes the rest of the filesystem outside writable roots read-only, rather than
inaccessible. Forge needs Git operations and provider-secret denial simultaneously.
The observed CLI exposes approval and network controls, but no qualified managed
policy for both requirements was established. `--yolo` and `--disable-sandbox` do
not solve that contract and must not be used. This is a documented mismatch,
not a claim that every possible Muse enterprise configuration has been tested.
[Official sandbox policy](https://dev.meta.ai/docs/muse-code/permissions),
[managed configuration surface](https://dev.meta.ai/docs/muse-code/configuration)

### Subscription and API boundaries

Current subscription documentation ties the plan to the API key automatically
connected by Muse CLI onboarding. Additional manually created API keys are metered
separately. An adapter must not label arbitrary API keys as subscription access.
[Official subscription documentation](https://dev.meta.ai/docs/muse-code/subscriptions)

The current terms restrict subscription credentials to Muse Code. They distinguish
standard services from discounted services whose submitted data can be used for
training, and restrict redistribution of downloadable materials. This spike does
not establish subscription automation/concurrency rights or permission to bundle
Muse into a distributed runner image. Resolve those points before enabling that
installation path. `*-contributor` models must be rejected by any future adapter
and configuration path. [Official terms](https://dev.meta.ai/legal/terms-of-service)

## Work required before enabling either adapter

First establish supported native isolation with synthetic secrets and ordinary
Git operations. Then implement the existing
[adapter contract](harness-adapters.md), retaining its shared deadline, cancellation,
credential refresh, capacity, and provider availability behavior. Do not add a
credential broker or remove sandboxing to obtain a passing probe.

After that boundary is established, authenticated qualification requires an
operator-selected account and authorization for the probe's model usage. It must
cover all role results, malformed/oversized output, quota/auth fallback, credential
refresh/reconnect, native shell/file tools, detached children, cancellation and
reset/reuse. Subscription accounting needs its own observed test. No login,
purchase, live credential use or billable request was performed by this spike.
