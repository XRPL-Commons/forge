# Public template defaults

Operational state, logs, credentials, and customer repository declarations belong
outside the source tree. Examples start disabled. The Git repository retains its
source history and author attribution; publication audits must check history as
well as current files.

`examples/factory.json.example` has no App connections or enabled repositories.
Its five-minute sync and fifteen-minute access expiry are configurable. Private
key environment references are documented in `examples/github-credentials.env.example`;
that file contains placeholder paths only. Real `factory.json`, credential environment
files, PEM files, and state are ignored by Git. See [GitHub settings](github-settings.md)
for setup order and the boundary between access readiness and live execution.

The public auditor template grants no administrative merge bypass. NORMAL and CRITICAL work must satisfy repository-required approvals and all other protections; CRITICAL work additionally needs a qualifying human review of the current candidate. Role instructions remain cooperative operating policy, not credential isolation.

`bin/forge setup` writes the user services for the checkout and runtime root it is given (default `~/.local/share/forge`); cloning the source alone installs nothing. The operator UI binds to loopback unless `--bind` names a private tailnet address, and requires a trusted private network.


The optional remote queue notification channel is named `relay`; configure the `RELAY_*` environment variables only if using that adapter. No channels are connected by this repository. The package manifest is private and pins browser-test tooling; it is not an npm publication package.

Provider isolation tools are experimental, opt-in components. Synthetic tests are not evidence that an arbitrary production installation is isolated. See [implementation status](implementation.md).

The durable factory's defaults live in `forge/factory/config.py`, and the disabled
`examples/runtime.json.example` restates them: 4 Build, 2 Review, 1 Audit and 1 Recovery
slot as per-role ceilings (memory admission decides how many run), and separate
dispatch/merge switches. `bin/forge setup` provisions the image, policies and internal
network; cloning source does not alter host security or start workers. See
[factory runtime](factory-runtime.md).
