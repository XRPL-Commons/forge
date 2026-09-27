import { readFileSync } from 'node:fs'
import { allowedHosts, guardAction, guardHost, proxyPaths, upstreamHeaders, readUpstreamSecret, responseHeaders, readCapped, MAX_RESPONSE_BODY, UPSTREAM_TIMEOUT_MS } from '../utils/proxy'

export default defineEventHandler(async (event) => {
  const path = event.path
  if (!proxyPaths(path)) return
  const req = event.node.req
  const headers = Object.fromEntries(Object.entries(req.headers).map(([k, v]) => [k, Array.isArray(v) ? v.join(',') : v]))
  // DNS rebinding: the request reached this process, but the name the browser
  // used has to be one this app is served on before anything is forwarded.
  // `process.env` is the configured bind; the socket is what Nitro actually
  // listened on, used only when HOST/PORT are not set.
  const listening = { address: req.socket?.localAddress ?? null, port: req.socket?.localPort ?? null }
  if (!guardHost(headers.host, allowedHosts(process.env, listening))) {
    setResponseStatus(event, 403)
    return { ok: false, reason: 'Untrusted host' }
  }
  const guard = guardAction({ method: req.method ?? 'GET', headers, contentLength: Number(headers['content-length'] ?? 0) })
  if (!guard.ok) { setResponseStatus(event, guard.status); return { ok: false, reason: guard.reason } }
  const config = useRuntimeConfig()
  const base = config.forgeApi.replace(/\/$/, '')
  const secret = readUpstreamSecret(config.forgeUpstreamSecretFile, path => readFileSync(path, 'utf8'))
  const upstreamHost = new URL(base).host
  // A Node Buffer is a valid fetch body at runtime; the DOM `BodyInit` type
  // just does not name it.
  const body = (req.method === 'POST' ? await readRawBody(event, false) : undefined) as BodyInit | undefined
  const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), UPSTREAM_TIMEOUT_MS)
  try {
    const upstream = await fetch(base + path, { method: req.method, headers: upstreamHeaders(headers, req.method ?? 'GET', upstreamHost, secret) as Record<string, string>,
      body, redirect: 'manual', signal: controller.signal })
    const result = await readCapped(upstream, MAX_RESPONSE_BODY, controller)
    if (!(result instanceof Buffer)) { setResponseStatus(event, 502); return { ok: false, reason: 'Upstream response too large' } }
    const buffer = result
    setResponseStatus(event, upstream.status)
    for (const [name, value] of responseHeaders(upstream.headers)) appendResponseHeader(event, name, value)
    return buffer
  } catch (error) {
    setResponseStatus(event, 502)
    return { ok: false, reason: 'Forge status service is unavailable. Check forge-status on the server.' }
  } finally { clearTimeout(timer) }
})
