# Operator UI deployment

`bin/forge setup` and `bin/forge update` install and update the operator UI.
There is no manual build or switch-over step. See
[Update Forge](installation.md#update-forge).

## Layout

| Service | Listens on | Runs |
|---|---|---|
| `forge-ui` | `<bind>:8787` | The Nitro build of `ui/`, in the pinned Node image (read-only, non-root, host network) |
| `forge-status` | `127.0.0.1:8788` | `legacy/forge-status.py`: the JSON API and the GitHub App callback |
| `forge-supervisor` | — | `bin/forge service` |

The status API accepts only requests carrying the secret in
`<runtime-root>/state/ui-upstream.secret` (0600, created by setup). The UI container
mounts it read-only and sends it on every proxied request, so another local process
or account cannot read the control token or act as the operator. The API refuses to
bind anything but loopback unless `FORGE_ALLOW_NETWORK_BIND=1` is set for a legacy
installation without the UI.

The UI proxies `/api/**` and `/onboarding/github/callback` to the status API (see
`ui/README.md`). It forwards the host name the browser used, after checking it, so
that GitHub App manifests send their callbacks back through the UI.

Releases live in `<runtime-root>/ui/releases/<digest>/`. Each contains the
self-contained `.output` bundle and the Help guides (the top-level `docs/*.md`,
read through `FORGE_ROOT=/app`).
`<runtime-root>/ui/current` points at the release in use. Icons are bundled
(`icon: { serverBundle: 'local' }`), so the UI fetches nothing at runtime.

Nitro answers a proxied request whose `Host` is neither `$HOST:$PORT` nor loopback
with 403 `Untrusted host`, as a DNS-rebinding defence. Reaching the UI under
another name, such as a MagicDNS name or a reverse proxy, needs
`NITRO_ALLOWED_HOSTS` (comma-separated `host:port` values). Add it with a systemd
drop-in (`systemctl --user edit forge-ui`), which update never overwrites.

## Moving a hand-built host to managed services

Hosts that followed the earlier manual runbook have their own `forge-ui.service`
and a `forge-status.service` edited for port 8788. Run `bin/forge update` once.
It shows each difference against the managed definition and asks before replacing
it. After that, update owns these units.
