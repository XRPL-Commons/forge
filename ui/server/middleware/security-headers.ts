// The Python server sent `Referrer-Policy: no-referrer` and
// `X-Content-Type-Options: nosniff` on the pages it served; Nitro is the
// front door now, so it has to send them itself.
//
// Nothing frames the UI, so every page also refuses to be framed. In
// Tailscale Serve mode, Serve attaches the operator's identity to every
// request from their device, including one made by a hidden cross-site
// iframe; without these headers that iframe could be used for clickjacking.
//
// Ordering does the filtering: server middleware runs in filename order and
// `forge-proxy.ts` sorts first, so anything it forwards to Python has already
// answered by the time this runs. What is left is what Nitro renders — the
// HTML pages, the /_ui/ routes, the bundled assets — and those get these
// headers. Proxied responses keep the upstream's own (PASS_RESPONSE in
// server/utils/proxy.ts passes them through).
export default defineEventHandler((event) => {
  setResponseHeader(event, 'Referrer-Policy', 'no-referrer')
  setResponseHeader(event, 'X-Content-Type-Options', 'nosniff')
  setResponseHeader(event, 'Content-Security-Policy', "frame-ancestors 'none'")
  setResponseHeader(event, 'X-Frame-Options', 'DENY')
})
