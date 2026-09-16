# Coding harness adapter contract v1

An adapter is trusted Forge installation code under `forge/harnesses/<id>/`.
It is reviewed and baked into an immutable runner image. A repository or model
response cannot install an adapter or supply a Python module path. There is no
runtime plugin downloader.

Each module contains:

- `manifest.json`: contract version, stable ID, display name, pinned package
  version, package binary, official terminal login arguments, relative credential
  file location, private credential reference, and API-key environment variable.
- `adapter.py`: `run(note, directory, env, result_schema)` and
  `probe(note, directory)` functions.
- Any adapter-specific canary or managed sandbox policy. Policies are image-owned
  and cannot be weakened by repository configuration.

The current build installer supports pinned npm distributions. A harness requiring
another distribution needs a reviewed installer extension with a fixed version
and integrity verification; never run an unpinned remote installer in a worker.

`run` receives a supervisor-owned assignment, the prepared checkout, and only that
execution's credential environment. It invokes the harness with native isolation,
explicit model selection when present, and structured output matching the supplied
schema. It atomically publishes at most 64 KiB to `/work/result.json` and returns
an exit code. A partial output or zero exit alone never advances the issue.

Availability errors must emit a bounded JSON event with `type: "error"` and a
normalized message identifying authentication or quota failure. Never print raw
credentials or provider envelopes. Ordinary execution failures retain their
distinct failure/investigation path. Provider output is not trusted GitHub proof.

`probe` must verify a real provider response and native tool isolation, including
denial of provider files/environment and writes outside the checkout. Only then
publish `{"ready": true, "attempt": "<assignment-id>"}`. A missing sandbox fails
closed; there is no bypass-permissions fallback.

The shared runner owns assignment persistence, credential injection, heartbeat,
deadline, process-group termination, container identity, forced teardown, credential
refresh preservation and capacity release. The supervisor owns issue state,
publication reconciliation, failure accounting, and the durable provider/model
round-robin cursor. These behaviors must not be duplicated inside adapters.

Before enabling a new module, qualify:

1. Structured outputs for all four roles and malformed/oversized output rejection.
2. Native shell and file-tool isolation, including malicious repository settings.
3. Authentication/quota fallback without spending an issue failure attempt.
4. Timeout, crash, detached children, interrupted login, reset and clean reuse.
5. Refresh persistence, reconnect races and no credentials in logs or source.
6. Explicit model selection and deterministic rotation across controller restarts.
7. Assigned GitHub credentials remain available to repository tools while provider
   credentials stay isolated, and those tools can reach GitHub through the proxy.
   Claude's setup probe verifies this using a synthetic token and a public endpoint.

OpenCode and Grok Build are follow-up adapters, not enabled providers until this
qualification succeeds.
