import { guardAction, proxyPaths, upstreamHeaders, responseHeaders, MAX_RESPONSE_BODY, UPSTREAM_TIMEOUT_MS } from '../utils/proxy'

export default defineEventHandler(async (event) => {
  const path = event.path
  if (!proxyPaths(path)) return
  const req = event.node.req
  const headers = Object.fromEntries(Object.entries(req.headers).map(([k, v]) => [k, Array.isArray(v) ? v.join(',') : v]))
  const guard = guardAction({ method: req.method ?? 'GET', headers, contentLength: Number(headers['content-length'] ?? 0) })
  if (!guard.ok) { setResponseStatus(event, guard.status); return { ok: false, reason: guard.reason } }
  const base = useRuntimeConfig().forgeApi.replace(/\/$/, '')
  const upstreamHost = new URL(base).host
  const body = req.method === 'POST' ? await readRawBody(event, false) : undefined
  const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), UPSTREAM_TIMEOUT_MS)
  try {
    const upstream = await fetch(base + path, { method: req.method, headers: upstreamHeaders(headers, req.method ?? 'GET', upstreamHost) as Record<string, string>,
      body, redirect: 'manual', signal: controller.signal })
    const buffer = Buffer.from(await upstream.arrayBuffer())
    if (buffer.length > MAX_RESPONSE_BODY) { setResponseStatus(event, 502); return { ok: false, reason: 'Upstream response too large' } }
    setResponseStatus(event, upstream.status)
    for (const [name, value] of responseHeaders(upstream.headers)) appendResponseHeader(event, name, value)
    return buffer
  } catch (error) {
    setResponseStatus(event, 502)
    return { ok: false, reason: 'Forge status service is unavailable. Check forge-status on the server.' }
  } finally { clearTimeout(timer) }
})
