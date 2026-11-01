# Prepared release assets

Published Forge releases include the operator UI and worker images. A fresh
Linux amd64 host downloads these assets instead of running `npm ci`, compiling
the UI or building worker images. The CLI stages the standard and full worker
archives so later environment preparation can continue without local GitHub
access. The full worker is loaded and qualified when database services are needed.

Each release is named `forge-build-<full commit SHA>`. Its `manifest.json` records:

- the exact source commit and Linux amd64 platform;
- the pinned Node and Python base images;
- fingerprints of the UI, documentation and worker build inputs;
- asset sizes and SHA-256 checksums;
- the complete Docker image IDs for the base and full workers.

The installer reads assets only from releases in `XRPL-Commons/forge`. It checks
the source, platform, pinned toolchain and build inputs, then verifies each
archive before extracting the UI or loading an image. Loaded images must have
the declared image ID and platform. The existing host isolation and environment
canaries still run before workers are enabled. A failed or unavailable download
never silently starts a source build.

The release publisher is the GitHub repository already trusted for Forge source.
The checksums establish artifact integrity and the manifest binds assets to their
source and build inputs; this is not a separate artifact signing authority.

## Publishing

The **Prepared release** workflow runs after **CI** succeeds for a push to this
repository's `main` branch. It checks out that CI run's exact commit, builds the
UI with the pinned Node container, builds both workers, and runs their environment
and isolation canaries. It creates a draft release with all assets and publishes
only after uploads succeed. Published releases are never overwritten. A retry
replaces an incomplete draft only when the draft belongs to `github-actions[bot]`
and names the exact tested commit. Human-owned drafts are left for inspection.
Each compressed asset must fit GitHub's 2 GiB release asset limit; the producer
checks this before publishing.

With no explicit revision, `forge install` and `forge self-update` select the
latest complete published prepared release and verify its manifest. An explicit
full commit SHA remains available for pinned recovery and development.

Only revisions with a published prepared release are ready for installation.
After merging, wait for the Prepared release workflow before installing that
revision.

## Private repository access

The CLI uses your local GitHub CLI login to read private release metadata and
obtain short-lived, read-only links for the exact assets. It sends those links
over the existing SSH connection. Your personal GitHub token stays local; it is
never sent to the server. Links stay in memory and are excluded from journals,
command arguments and normal output.

The host immediately downloads and verifies each archive into a private cache
at `~/.cache/forge/prepared/<revision>`. Fresh installation stages the cache under
the installer before the Forge service account exists, then transfers the
verified files to that account. Interrupted transfers do not become usable cache
entries. Resume reuses verified files and obtains fresh download links for any
remaining transfers. Worker and UI activation still checks the manifest against
the exact source, toolchain and build inputs.

Use the local CLI for installing or updating a private Forge release. A direct
host-side update needs an already staged cache or an explicitly selected source
build; the host cannot fetch private releases using your personal credentials.

## Development builds

Maintainers can deliberately build a local source checkout by setting
`FORGE_BUILD_FROM_SOURCE=1` for the host-side setup/update command. The release
producer uses the same explicit setting while building assets. This mode retains
the existing build and host qualification path; it is slower and is not an
automatic fallback for an unpublished release. Other platforms currently need
this development path.

To build release files on a clean Linux amd64 checkout with Docker, sudo and
AppArmor available:

```sh
python3 tools/build_release_artifacts.py --output /tmp/forge-prepared
```

The destination must be empty. The command prepares and tests files locally; it
does not publish a release or change GitHub permissions.
