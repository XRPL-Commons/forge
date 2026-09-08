# Contributing

Open an issue describing the concrete behavior you want to change. Include
synthetic examples or redacted diagnostics; never post credentials, raw private
worker output, or another repository's private source.

Keep issues as the unit of pipeline progress. Distinguish observed activity from
dispatch, and distinguish a merged PR from confirmed issue completion. Changes to
worker retirement, ownership, or human approval checks need regression tests that
exercise the failure cases, not just the successful path.

Run the Python suites and the browser checks relevant to your changes. Tests must
not rely on live GitHub mutations or send external notifications. Keep runtime
state, configuration, logs, database files, and authentication outside version
control. Describe the problem, resulting behavior, and validation in pull requests.
