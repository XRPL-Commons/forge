# Provider API-key broker qualification

The runner routes installation-owned Claude API-key accounts through a host
broker. Pipeline jobs, discussions and setup probes share this path. Browser
sign-in and Grok are unchanged. No browser account is converted to API billing.

Native Linux qualification of the complete boundary and Bash credential
isolation must pass for the deployed image (see below). Browser credential
isolation and Grok support are not covered.

## Proven behavior

The pinned Claude Code 2.1.273 binary completed a native Read tool call, sent its
actual file result in the next request, and returned a schema-constrained result
through the broker. The entire test ran with Docker networking disabled, a fresh
home, and synthetic credentials. No provider API was contacted.

The observed inference route was `POST /v1/messages?beta=true`. The test exercised
real Claude SSE parsing, the Read tool, StructuredOutput, and host credential
injection. The fake upstream received the host API-key fixture, not the worker's
job bearer capability. API-key provider acceptance remains a separate deployment
qualification; this test does not establish browser subscription compatibility.

Grok 1.0.34 could not start its strict sandbox under the available
ARM Docker host's x86 emulation. Its namespace lockdown returned ENOSYS. The test
did not disable its sandbox. Qualify Grok on native x86 Linux before implementing
its routes; no Grok support is claimed by this module.

## Broker boundary

`forge.provider_proxy.Assignment` fixes execution ID, account generation, model,
expiry and a random job capability. The lifecycle owner supplies a host-only
API-key loader and `authorized(assignment)`. That callback must read current
execution fencing, cleanup, installation generation, assigned provider/account,
and account generation on every call. It must not retain a database snapshot
across requests or chunks.

Each execution gets one private Unix socket, mounted only into its own gateway.
The gateway streams HTTP to that socket; it cannot choose another account or job.
The worker authenticates using its job capability, not an Anthropic credential.
The broker calls only `api.anthropic.com` and refuses redirects. Request models
must match the assigned concrete model ID exactly. Messages and count_tokens
routes are allowed; arbitrary URLs and other endpoints are refused.

The broker replaces caller authentication with the host API key. It preserves
request bodies, Anthropic capability/version headers, session headers, response
SSE bytes, rate-limit headers and retry metadata. It does not log prompts, keys,
or response bodies. It bounds concurrent requests, request/response bytes, idle
time and total request time. Fencing or account removal stops an active stream:
a watchdog shuts down an idle upstream socket within 250 ms of its next successful
status check, and each chunk is checked again before forwarding. Requests also end
at the earlier of their execution deadline or 600 seconds. The live callback must
itself be bounded; database failure withdraws authority.

## Runtime lifecycle

The existing `forge-mirror` service owns inference sockets too. Each private
persisted grant captures the current SQL execution fence, immutable manifest,
account generation, concrete model, deadline and random capability. It uses the
same authority database as its worker, including disposable role qualifications.
Account replacement, fencing, capture or cleanup withdraws existing streams.
Service restart recreates the socket with the original capability, so live
workers retain their bounded grant. Cleanup removes the grant before teardown.

The worker envelope contains `ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN` (the
job capability). It contains no account key or refresh material. A readiness
request checks the live broker again before credentials are handed off and before
the worker starts. The gateway mounts only that job's socket, exposes
`http://forge-provider:8082`, and refuses every provider CONNECT route in this
mode. Registry downloads remain available. Setup probes use this same isolated
boundary with a retained-volume keeper and no Git frontend.

The managed Claude sandbox denies the capability to shell tools and lifecycle
scripts. Capture retains the exact capability in private host scrub state before
handoff. The native Bash test below must establish that the pinned image enforces
this policy; configuration alone is not proof.

### Models

A fake-upstream trace of Claude 2.1.273 observed these API-key selections:

| Configured model | Captured concrete model |
| --- | --- |
| Default (`null`) | `claude-opus-5` |
| `sonnet` | `claude-sonnet-5` |
| `opus` | `claude-opus-5` |
| `haiku` | `claude-haiku-4-5-20251001` |

Explicit `claude-*` IDs pass through. Other aliases require qualification rather
than silently selecting a different model. The adapter receives the captured
concrete ID. Updating the pinned binary requires repeating
`tests/qualification/claude_models.py`; a version mismatch refuses proxy jobs.

### Native qualification through the CLI

After installing the matching source and worker image on a disposable Linux
host, run:

```sh
forge setup qualify-boundary --ssh anvil --provider-proxy
forge setup qualify-boundary --ssh anvil --provider-proxy --apply
```

This uses the installed service account and exact image. A host fake upstream
supplies a real Claude Bash tool call and structured output. It consumes and deletes the same private credential envelope as production; the
read-only assignment contains no capability. It checks shell environment absence,
private-file denial, a separate PID namespace and procfs, alternate process-root
paths, host-only key injection, direct-provider denial, another
job's capability, revocation, and verified resource cleanup. It uses synthetic
credentials and makes no provider API requests. It requires the normal Forge
private-proc AppArmor policy; it never disables the sandbox to obtain a pass.
The returned receipt identifies the image and source revision. A failed or
unperformed native run is not qualification.

## Reproduce the local qualification

Use a built Forge runner image containing Claude 2.1.273. The script checks the
version and fails unless the tool result and structured result are observed.

```sh
python3 -m unittest tests.test_provider_proxy -v

docker run --rm --network none \
  --tmpfs /work:uid=1000,gid=1000,mode=700 \
  --mount type=bind,src="$PWD/tests/qualification/provider_proxy.py",dst=/test.py,readonly \
  --mount type=bind,src="$PWD/forge/provider_proxy.py",dst=/provider_proxy.py,readonly \
  --entrypoint python3 "$FORGE_TEST_RUNNER_IMAGE" /test.py
```

The script provides its own fake upstream and supplies no real
credentials. Existing user homes and workspaces are not mounted.

Official protocol reference:
https://code.claude.com/docs/en/llm-gateway-protocol
