import type { IncomingMessage } from 'node:http'
export const MAX_ACTION_BODY = 262144
export const MAX_RESPONSE_BODY = 1024 * 1024
export const UPSTREAM_TIMEOUT_MS = 30000
// `content-disposition` carries the ruleset download's filename
// (`forge-review-rules-<id>.json`, contracts §2); dropping it renames the file.
// `referrer-policy` and `x-content-type-options` are the upstream's own
// hardening headers; dropping them would silently weaken every proxied
// response relative to what the Python server sends directly.
const PASS_RESPONSE = ['content-type', 'content-disposition', 'set-cookie', 'location', 'cache-control',
  'referrer-policy', 'x-content-type-options', 'etag', 'x-forge-request-id', 'server-timing']

type Headers = Record<string, string | undefined>
interface GuardInput { method: string; headers: Headers; contentLength?: number; publicOrigin?: string }
type GuardResult = { ok: true } | { ok: false; status: number; reason: string }

export function proxyPaths(path: string): boolean {
  const clean = path.split('?')[0] ?? ''
  // @nuxt/icon serves the bundled Lucide collection under /api/_nuxt_icon/…;
  // that is Nitro's own route, not the Forge status service's.
  if (clean === '/api/_nuxt_icon' || clean.startsWith('/api/_nuxt_icon/')) return false
  return clean === '/api' || clean.startsWith('/api/') || clean === '/onboarding/github/callback' || clean === '/onboarding/cli/github/start' || clean === '/onboarding/cli/github/callback' || clean === '/auth/github/start' || clean === '/auth/github/callback'
}

// DNS-rebinding defence. A page on a name the attacker controls can be made
// to resolve to this server's address; the browser then sends same-origin
// requests whose `Host` is that name. Comparing `Host` against the addresses
// this app is actually served on rejects them before the proxy forwards
// anything to the Forge status service.
export function guardHost(hostHeader: string | undefined, allowed: string[]): boolean {
  const host = (hostHeader ?? '').trim().toLowerCase()
  if (!host) return false
  return allowed.some(entry => entry.trim().toLowerCase() === host)
}

// Node reports an IPv4 loopback socket as `::ffff:127.0.0.1` and an IPv6 one
// as `::1`; the Host header never carries the first form and brackets the
// second.
function hostLabel(address: string): string {
  const plain = address.startsWith('::ffff:') ? address.slice(7) : address
  return plain.includes(':') ? `[${plain}]` : plain
}

interface ListenAddress { address?: string | null; port?: number | null }

// The allowed `Host` values: `NITRO_ALLOWED_HOSTS` verbatim when set
// (comma-separated, for deployments reached under another name or port),
// else the configured bind address and port plus loopback. Bare `localhost`
// and `127.0.0.1` are included because Nitro's SSR-internal `$fetch` calls
// the h3 app through `http://localhost/` with no port; a browser can only
// send a port-less Host when the server is on port 80, which this app is not.
export function allowedHosts(env: Record<string, string | undefined>, listening?: ListenAddress): string[] {
  const override = (env.NITRO_ALLOWED_HOSTS ?? '').split(',').map(entry => entry.trim()).filter(Boolean)
  if (override.length) return override
  const port = String(env.PORT ?? env.NITRO_PORT ?? listening?.port ?? '3000').trim()
  const hosts = new Set<string>(['localhost', '127.0.0.1', `localhost:${port}`, `127.0.0.1:${port}`, `[::1]:${port}`])
  const bind = (env.HOST ?? env.NITRO_HOST ?? '').trim()
  const reported = (listening?.address ?? '').trim()
  for (const candidate of [bind, reported]) {
    if (!candidate || candidate === '0.0.0.0' || candidate === '::') continue
    hosts.add(`${hostLabel(candidate)}:${port}`)
  }
  return [...hosts]
}

export function guardAction({ method, headers, contentLength, publicOrigin }: GuardInput): GuardResult {
  if (method !== 'POST') return { ok: true }
  const host = headers.host ?? ''
  if (!host || headers.origin !== (publicOrigin || 'http://' + host) || (publicOrigin && new URL(publicOrigin).host !== host)) return { ok: false, status: 403, reason: 'Cross-origin action rejected' }
  // Browsers only send Sec-Fetch-* to trustworthy origins (https, localhost); the UI runs over plain
  // http on the tailnet, so an absent header is accepted and only an explicit non-same-origin value rejected.
  const site = headers['sec-fetch-site']
  if (site !== undefined && site !== '' && site !== 'same-origin') return { ok: false, status: 403, reason: 'Cross-site action rejected' }
  if (!headers['x-forge-action']) return { ok: false, status: 403, reason: 'Action token missing' }
  if (!((headers['content-type'] ?? '').split(';')[0] ?? '').trim().startsWith('application/json')) return { ok: false, status: 415, reason: 'Actions are JSON' }
  const size = guardBodyLength(contentLength, MAX_ACTION_BODY)
  if (!size.ok) return size
  return { ok: true }
}

// Tailscale Serve adds `Tailscale-User-Login` for a tailnet person (never for a
// tagged device). It is an identity only when this process received it from
// Serve itself: Serve connects over loopback to the configured HTTPS name.
// Any other path, such as a direct request to the tailnet address, may carry a
// forged header, so nothing is forwarded. The status API trusts only
// `TAILSCALE_UPSTREAM_HEADER`, and `upstreamHeaders` never copies a client's.
export const TAILSCALE_UPSTREAM_HEADER = 'x-forge-tailscale-user-login'
const TAILSCALE_LOGIN = /^[a-z0-9][a-z0-9._%+-]{0,63}@[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?)*$/

function loopback(address: string | undefined): boolean {
  const plain = (address ?? '').startsWith('::ffff:') ? (address ?? '').slice(7) : (address ?? '')
  return plain === '::1' || /^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(plain)
}

export function tailscaleIdentity(headers: Headers, remoteAddress: string | undefined, env: Record<string, string | undefined>): string | undefined {
  if (env.NITRO_FORGE_TAILSCALE_IDENTITY !== 'serve') return undefined
  let origin: URL
  try { origin = new URL(env.NITRO_FORGE_PUBLIC_ORIGIN ?? '') } catch { return undefined }
  if (origin.protocol !== 'https:' || origin.pathname !== '/' || !loopback(remoteAddress)) return undefined
  if ((headers.host ?? '').trim().toLowerCase() !== origin.host.toLowerCase()) return undefined
  // A repeated header arrives comma-joined; that is never one person.
  const value = (headers['tailscale-user-login'] ?? '').trim().toLowerCase()
  if (!value || value.includes(',') || value.startsWith('tagged-devices') || value.length > 320 || !TAILSCALE_LOGIN.test(value)) return undefined
  return value
}

// `x-forwarded-host` is the name the browser used, already checked by
// guardHost. The status service builds GitHub App manifest URLs from it, so
// App callbacks return through this front door rather than to the loopback
// upstream address.
export function upstreamHeaders(headers: Headers, method: string, upstreamHost: string, secret = '', client = false, tailscaleLogin?: string): Headers {
  const out: Headers = { host: upstreamHost }
  // Operator identity from Serve, never on agent (bearer) routes.
  if (tailscaleLogin && !client) out[TAILSCALE_UPSTREAM_HEADER] = tailscaleLogin
  // The status API refuses requests without it: another local process cannot
  // read this 0600 file, so it cannot act as the operator or forge the host.
  if (secret) out['x-forge-upstream'] = secret
  if (headers.host) out['x-forwarded-host'] = headers.host
  for (const name of ['cookie', 'content-type', 'x-forge-action', 'x-forge-request-id', 'accept', 'if-none-match']) if (headers[name]) out[name] = headers[name]
  if (client && headers.authorization) out.authorization = headers.authorization
  if (method === 'POST' && !client) { out.origin = 'http://' + upstreamHost; out['sec-fetch-site'] = 'same-origin' }
  return out
}

export function responseHeaders(from: globalThis.Headers): [string, string][] {
  const out: [string, string][] = []
  from.forEach((value, name) => { if (PASS_RESPONSE.includes(name.toLowerCase())) out.push([name, value]) })
  return out
}

// Reads the upstream body in chunks, bailing out (and aborting the upstream
// request) as soon as the running total exceeds `limit`, so we never buffer
// more than ~limit bytes for an oversized response. On any exit that isn't a
// clean `done` — the too-large bail-out, or the reader rejecting — the reader
// is cancelled and its lock released in `finally` so the stream is never left
// dangling.
export async function readCapped(response: Response, limit: number, controller: AbortController): Promise<Buffer | { tooLarge: true }> {
  const body = response.body
  if (!body) return Buffer.alloc(0)
  const reader = body.getReader()
  const chunks: Uint8Array[] = []
  let total = 0
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      if (!value) continue
      total += value.length
      if (total > limit) {
        controller.abort()
        reader.cancel().catch(() => {})
        return { tooLarge: true }
      }
      chunks.push(value)
    }
    return Buffer.concat(chunks)
  } catch (error) {
    reader.cancel().catch(() => {})
    throw error
  } finally {
    try { reader.releaseLock() } catch { /* already released or stream errored */ }
  }
}

// Read per request (a few bytes) so a secret created after the UI started is used.
export function readUpstreamSecret(path: string, read: (path: string) => string): string {
  if (!path) return ''
  try { return read(path).trim() } catch { return '' }
}

// Only these closed client routes bypass browser CSRF. Ambient browser
// credentials and Origin are rejected; the Python service verifies client grants.
export function clientPath(path: string): boolean {
  return ['/api/client/device', '/api/client/token', '/api/client/refresh', '/api/client/status',
    '/api/client/disconnect', '/api/client/requirements', '/api/client/authorize',
    '/api/client/read', '/api/client/operation', '/api/client/policy', '/api/client/planning', '/api/client/delivery'].includes(path)
}
export function guardClient({ method, headers, contentLength }: GuardInput): GuardResult {
  if (method !== 'POST') return { ok: false, status: 405, reason: 'Client requests use POST' }
  if (headers.origin || headers.cookie) return { ok: false, status: 403, reason: 'Use local client credentials' }
  if ((headers['content-type'] ?? '').split(';')[0]?.trim() !== 'application/json') return { ok: false, status: 415, reason: 'Client requests are JSON' }
  const size = guardBodyLength(contentLength, 16384)
  if (!size.ok) return size
  return { ok: true }
}


function guardBodyLength(length: number | undefined, maximum: number): GuardResult {
  // A missing length is a bounded stream, not an oversized request.
  if (length === undefined) return { ok: true }
  if (!Number.isSafeInteger(length) || length < 0) return { ok: false, status: 400, reason: 'Invalid request length' }
  if (length === 0) return { ok: false, status: 400, reason: 'Request body is empty' }
  return length > maximum ? { ok: false, status: 413, reason: 'Action body too large' } : { ok: true }
}

export class RequestBodyError extends Error {
  constructor(public status: number, message: string) { super(message) }
}

export function readRequestBody(request: IncomingMessage, maximum: number, signal: AbortSignal): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = []
    let length = 0
    const clean = () => {
      request.off('data', data); request.off('end', end); request.off('aborted', aborted); request.off('error', aborted)
      signal.removeEventListener('abort', timeout)
    }
    const fail = (status: number, message: string) => {
      clean(); request.pause(); reject(new RequestBodyError(status, message))
    }
    const data = (chunk: Buffer | string) => {
      const value = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)
      length += value.length
      if (length > maximum) return fail(413, 'Action body too large')
      chunks.push(value)
    }
    const end = () => { clean(); length ? resolve(Buffer.concat(chunks, length)) : reject(new RequestBodyError(400, 'Request body is empty')) }
    const aborted = () => fail(400, 'Request body was interrupted')
    const timeout = () => fail(408, 'Request body timed out')
    if (signal.aborted) return timeout()
    if (request.aborted || request.destroyed) return aborted()
    request.on('data', data); request.once('end', end); request.once('aborted', aborted); request.once('error', aborted)
    signal.addEventListener('abort', timeout, { once: true })
  })
}
