# Claude failure diagnostics

Claude's adapter keeps Forge's result file as the only completion authority. A
failed CLI invocation emits the existing `type: error` event and fixed message,
plus `provider_failure` metadata in the normal runner log:

- Process exit code and terminal-result presence.
- Documented terminal subtype, `is_error`, structured-output presence, and the
  terminal `api_error_status` when provided.
- The latest typed assistant API error and `system/api_retry` observation, using
  only known error categories and numeric status/retry fields. These are
  earlier observations, not inferred terminal causes.
- A reason distinguishing startup schema rejection, exhausted structured-output
  retries, missing terminal result, missing structured output, terminal error,
  or a nonzero process exit despite a structured response.
- At most three complete terminal `errors` strings, each at most 1,024 bytes
  after JSON encoding, with total and omitted counts. An oversized error is
  omitted whole. The documented startup schema-error line uses the same bound.

Model result text, session identifiers, tool results, and other transcript lines
are excluded. The existing runner scrubs delivered credentials (including their
JSON-escaped forms) before retaining its bounded log tail. Diagnostics do not
read credential files or add another redaction owner. Triage keeps these logs
under its existing 30-day raw-log retention policy; diagnostics never create a
successful result or grant publication authority. Auth and quota classifications
are unchanged.

Claude documents a nonzero exit for failed runs and the explicit invalid-schema
startup error in [programmatic CLI usage](https://code.claude.com/docs/en/headless).
Its [structured-output documentation](https://code.claude.com/docs/en/agent-sdk/structured-outputs)
distinguishes retry exhaustion from a successful terminal result lacking a
structured output, and recommends examining terminal `errors` to distinguish
validation failures from output retraction. These diagnostics preserve that
evidence without treating an absent result as proof of a schema problem.

The [CLI retry event contract](https://code.claude.com/docs/en/headless#handle-api-retries)
and the pinned CLI stream schemas define the selected API error categories and
numeric fields. Assistant content, `error_details`, and unconfirmed generic
`type: error` input shapes are excluded.
