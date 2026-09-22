# Deploy the operator UI (forge-ui)

The Nuxt/Nitro app (`ui/`) becomes the tailnet-facing front door on port
8787. The Python status server (`forge-status`) moves to loopback-only on
port 8788, and Nitro proxies `/api/**` and `/onboarding/github/callback` to
it (see `ui/README.md`).

Only `ui/.output/` — the self-contained Nitro build — plus `ui/package.json`
need to exist under the live root (`~/ops/forge-watchdog/ui`). `.output/`
bundles its own server code and dependencies; `node_modules` is not needed
there. `NITRO_HELP_DOC` points at `%h/ops/forge-watchdog/docs/onboarding.md`,
so that file must also exist under the live root (it already does — it is
part of the watchdog copy of the repo).

No runtime dependency on the Iconify API: Nuxt UI draws its own
`i-lucide-*` icons, and `icon: { serverBundle: 'local' }` compiles
`@iconify-json/lucide` into `.output/`, so `/api/_nuxt_icon/**` is served
from disk and nothing is fetched over the network.

Nitro answers a proxied request whose `Host` is neither `$HOST:$PORT` nor
loopback with 403 `Untrusted host` (a DNS-rebinding defence); set
`NITRO_ALLOWED_HOSTS` — comma-separated `host:port` values — when the UI is
reached under any other name, such as a MagicDNS name or a reverse proxy.

## Switch-over steps

1. `cd ~/forger-remote/ui && npm ci && npm run build`
2. `mkdir -p ~/ops/forge-watchdog/ui && rsync -a --delete ~/forger-remote/ui/.output/ ~/ops/forge-watchdog/ui/.output/ && cp ~/forger-remote/ui/package.json ~/ops/forge-watchdog/ui/`
   (copy `ui/` except `node_modules`)
3. `cp deploy/forge-ui.service.example ~/.config/systemd/user/forge-ui.service`;
   edit `HOST` to the box's Tailscale IPv4 (copy the value from
   `tailscale ip -4`)
4. Add the two `Environment=` lines to
   `~/.config/systemd/user/forge-status.service`
   (`FORGE_BIND_ADDRESS=127.0.0.1`, `FORGE_PORT=8788`)
5. `systemctl --user daemon-reload && systemctl --user restart forge-status && systemctl --user enable --now forge-ui`
6. ```
   curl -s -o /dev/null -w '%{http_code}\n' http://<tailscale-ip>:8787/   → 200 (or 303 to /onboarding)
   curl -s http://<tailscale-ip>:8787/api/factory | head -c 80   → {"ok": true, …}
   ```

## Rollback

```
systemctl --user disable --now forge-ui
# remove the two Environment= lines from forge-status.service
systemctl --user restart forge-status
```

This restores the Python server on port 8787 with the previous bind address
and leaves `forge-ui` stopped and disabled.
