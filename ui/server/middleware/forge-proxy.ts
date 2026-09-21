import { guardAction, proxyPaths, upstreamHeaders, responseHeaders, MAX_RESPONSE_BODY, UPSTREAM_TIMEOUT_MS } from '../utils/proxy'

// Reads the upstream body in chunks, bailing out (and aborting the upstream
// request) as soon as the running total exceeds `limit`, so we never buffer
// more than ~limit bytes for an oversized response.
async function readCapped(response: Response, limit: number, controller: AbortController): Promise<Buffer | { tooLarge: true }> {
  const body = response.body
  if (!body) return Buffer.alloc(0)
  const reader = body.getReader()
  const chunks: Uint8Array[] = []
  let total = 0
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    if (!value) continue
    total += value.length
    if (total > limit) {
      reader.cancel().catch(() => {})
      controller.abort()
      return { tooLarge: true }
    }
    chunks.push(value)
  }
  return Buffer.concat(chunks)
}

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
