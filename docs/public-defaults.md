# Public template defaults

Operational state, logs, credentials, and customer repository declarations belong
outside the source tree. Examples start disabled. The Git repository retains its
source history and author attribution; publication audits must check history as
well as current files. Configure the legacy worker bot identity explicitly with
`RECOVERY_AUTHOR` before enabling live execution; the neutral `forge-bot` fallback
is for compatibility, not identity discovery.

`examples/factory.json.example` has no App connections or enabled repositories.
Its five-minute sync and fifteen-minute access expiry are configurable. Private
key environment references are documented in `examples/github-credentials.env.example`;
that file contains placeholder paths only. Real `factory.json`, credential environment
files, PEM files, and state are ignored by Git. See [GitHub settings](github-settings.md)
for setup order and the boundary between access readiness and live execution.

The public auditor template grants no administrative merge bypass. NORMAL and CRITICAL work must satisfy repository-required approvals and all other protections; CRITICAL work additionally needs a qualifying human review of the current candidate. Role instructions remain cooperative operating policy, not credential isolation.

Service templates use the current user's home (`%h`), and Python defaults resolve `Path.home()`. They assume an operator-managed flat installation under `~/ops/forge-watchdog`; cloning the source alone does not install services or relocate the compatibility scripts. Configure the paths for your installation. The dashboard binds to the machine's Tailscale address and requires a trusted private network.

Model triage is opt-in. Service templates default to local rules and disable model data authorization. The supervisor defaults to three fleet sessions and one per role, with conservative pressure guardrails. These are starting settings, not a sizing guarantee.

The optional remote queue notification channel is named `relay`; configure the `RELAY_*` environment variables only if using that adapter. No channels are connected by this repository. The package manifest is private and pins browser-test tooling; it is not an npm publication package.

Provider isolation and executor integration tools are experimental, opt-in components. Synthetic tests are not evidence that an arbitrary production installation is isolated. See [implementation status](implementation.md).
