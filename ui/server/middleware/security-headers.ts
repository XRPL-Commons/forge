// The Python server sent `Referrer-Policy: no-referrer` and
// `X-Content-Type-Options: nosniff` on the pages it served; Nitro is the
// front door now, so it has to send them itself.
//
// Ordering does the filtering: server middleware runs in filename order and
// `forge-proxy.ts` sorts first, so anything it forwards to Python has already
// answered by the time this runs. What is left is what Nitro renders — the
// HTML pages, the /_ui/ routes, the bundled assets — and those get both
// headers. Proxied responses keep the upstream's own (PASS_RESPONSE in
// server/utils/proxy.ts passes both through).
export default defineEventHandler((event) => {
  setResponseHeader(event, 'Referrer-Policy', 'no-referrer')
  setResponseHeader(event, 'X-Content-Type-Options', 'nosniff')
})
