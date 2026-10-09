import { readFileSync } from 'node:fs'
import { compressJson } from '../utils/compression'
import { allowedHosts, clientPath, guardClient, guardAction, guardHost, proxyPaths, upstreamHeaders, readUpstreamSecret, responseHeaders, readCapped, readRequestBody, RequestBodyError, MAX_ACTION_BODY, MAX_RESPONSE_BODY, UPSTREAM_TIMEOUT_MS } from '../utils/proxy'

export default defineEventHandler(async (event) => {
  const path = event.path
  if (!proxyPaths(path)) return
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), UPSTREAM_TIMEOUT_MS)
  try {
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
    const client = clientPath(path)
    const guard = (client ? guardClient : guardAction)({ method: req.method ?? 'GET', headers, contentLength: headers['content-length'] === undefined ? undefined : Number(headers['content-length']), publicOrigin: process.env.NITRO_FORGE_PUBLIC_ORIGIN })
    if (!guard.ok) { setResponseStatus(event, guard.status); return { ok: false, reason: guard.reason } }
    const config = useRuntimeConfig()
    const base = config.forgeApi.replace(/\/$/, '')
    const secret = readUpstreamSecret(config.forgeUpstreamSecretFile, path => readFileSync(path, 'utf8'))
    const upstreamHost = new URL(base).host
    // A Node Buffer is a valid fetch body at runtime; the DOM `BodyInit` type
    // just does not name it.
    const body = (req.method === 'POST' ? await readRequestBody(req, client ? 16384 : MAX_ACTION_BODY, controller.signal) : undefined) as BodyInit | undefined
    const upstream = await fetch(base + path, { method: req.method, headers: upstreamHeaders(headers, req.method ?? 'GET', upstreamHost, secret, client) as Record<string, string>,
      body, redirect: 'manual', signal: controller.signal })
    const result = await readCapped(upstream, MAX_RESPONSE_BODY, controller)
    if (!(result instanceof Buffer)) { setResponseStatus(event, 502); return { ok: false, reason: 'Upstream response too large' } }
    setResponseStatus(event, upstream.status)
    for (const [name, value] of responseHeaders(upstream.headers)) appendResponseHeader(event, name, value)
    if (req.method === 'GET' && ['/api/issues', '/api/factory', '/api/factory/changes'].includes(path.split('?')[0] ?? '')) {
      appendResponseHeader(event, 'vary', 'Accept-Encoding')
      const compressed = await compressJson(result, headers['accept-encoding'], upstream.headers.get('content-type'), upstream.status)
      if (compressed) { setResponseHeader(event, 'content-encoding', 'gzip'); return compressed }
    }
    return result
  } catch (error) {
    if (error instanceof RequestBodyError) {
      setResponseStatus(event, error.status)
      setResponseHeader(event, 'connection', 'close')
      return { ok: false, reason: error.message }
    }
    setResponseStatus(event, 502)
    return { ok: false, reason: 'Forge status service is unavailable. Check forge-status on the server.' }
  } finally { clearTimeout(timer) }
})
