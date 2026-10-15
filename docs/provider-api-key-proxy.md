# Provider API-key broker qualification

This module is an integration candidate, not an active runtime path. It supports
Claude API-key assignments only. Browser sign-in and Grok are unchanged.
Do not close #265 until the lifecycle wiring and deployed isolation checks below
are complete.

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

Grok 1.0.34 (3736acbc8658) could not start its strict sandbox under the available
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

## Shared wiring proposal

These edits are intentionally left to the integrator because another lane owns
the gateway and service lifecycle:

1. Activate this path only for an explicitly selected Claude API-key account.
   Never select it for `provider_auth_file_env` or `CLAUDE_CODE_OAUTH_TOKEN`.
   Keep the existing browser path and its billing semantics unchanged.
2. The host creates the assignment and socket before worker startup. Bind the
   account generation and concrete model to the execution's captured assignment.
   Generate the capability on the host; store no provider key in the assignment.
3. Replace API-key credential handoff with `claude_environment(...,
   auth_mode='api_key')`. The envelope contains only base URL and job capability;
   no provider API key, auth file, refresh material or auth write-back digest.
4. Add a streaming gateway route (suggested local origin
   `http://forge-provider:8082`) to the mounted per-execution Unix socket. Preserve
   response bytes incrementally. For this mode, refuse all direct provider
   CONNECT routes. Do not leave the old Anthropic egress route available.
5. Scrub the job capability from repository tool subprocesses as well as logs.
   The capability is a bounded inference grant and must not become a general
   repository credential. Existing sandbox policies and private-proc isolation
   remain enabled.
6. Reconstruct or withdraw sockets after host restart using current execution
   authority. Fence before cleanup/reset, shut down the server, and remove its
   socket. Disable credential write-back for proxy-managed executions.
7. Add integration tests for account-generation changes, fencing, cleanup,
   restart, no key in the worker envelope and direct-egress refusal. Qualify the
   pinned CLI in the complete production topology before enabling the mode.

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

Validated locally using image `forge-runner:e8c319a511e453c8` (image ID begins
`58004769ca52`). The script provides its own fake upstream and supplies no real
credentials. Existing user homes and workspaces are not mounted.

Official protocol reference:
https://code.claude.com/docs/en/llm-gateway-protocol
