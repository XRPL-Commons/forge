# Public template defaults

This source snapshot contains no operational state, logs, credentials, repository declarations, Git history, or deployment receipts. Examples start disabled. Configure the worker bot identity explicitly with `RECOVERY_AUTHOR` before enabling live execution; the neutral `forge-bot` fallback is for compatibility, not identity discovery.

The public auditor template grants no administrative merge bypass. NORMAL and CRITICAL work must satisfy repository-required approvals and all other protections; CRITICAL work additionally needs a qualifying human review of the current candidate. Role instructions remain cooperative operating policy, not credential isolation.

Service templates use the current user's home (`%h`), and Python defaults resolve `Path.home()`. They assume an operator-managed flat installation under `~/ops/forge-watchdog`; cloning the source alone does not install services or relocate the compatibility scripts. Configure the paths for your installation. The dashboard binds to the machine's Tailscale address and requires a trusted private network.

Model triage is opt-in. Service templates default to local rules and disable model data authorization. The supervisor defaults to three fleet sessions and one per role, with conservative pressure guardrails. These are starting settings, not a sizing guarantee.

The optional remote queue notification channel is named `relay`; configure the `RELAY_*` environment variables only if using that adapter. No channels are connected by this repository. The package manifest is private and pins browser-test tooling; it is not an npm publication package.

Provider isolation and executor integration tools are experimental, opt-in components. Synthetic tests are not evidence that an arbitrary production installation is isolated. See [implementation status](implementation.md).
