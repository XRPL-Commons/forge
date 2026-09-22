export const MAX_ACTION_BODY = 262144
export const MAX_RESPONSE_BODY = 1024 * 1024
export const UPSTREAM_TIMEOUT_MS = 30000
// `content-disposition` carries the ruleset download's filename
// (`forge-review-rules-<id>.json`, contracts §2); dropping it renames the file.
// `referrer-policy` and `x-content-type-options` are the upstream's own
// hardening headers; dropping them would silently weaken every proxied
// response relative to what the Python server sends directly.
const PASS_RESPONSE = ['content-type', 'content-disposition', 'set-cookie', 'location', 'cache-control',
  'referrer-policy', 'x-content-type-options']

type Headers = Record<string, string | undefined>
export interface GuardInput { method: string; headers: Headers; contentLength: number }
export type GuardResult = { ok: true } | { ok: false; status: number; reason: string }

export function proxyPaths(path: string): boolean {
  const clean = path.split('?')[0] ?? ''
  // @nuxt/icon serves the bundled Lucide collection under /api/_nuxt_icon/…;
  // that is Nitro's own route, not the Forge status service's.
  if (clean === '/api/_nuxt_icon' || clean.startsWith('/api/_nuxt_icon/')) return false
  return clean === '/api' || clean.startsWith('/api/') || clean === '/onboarding/github/callback'
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

export interface ListenAddress { address?: string | null; port?: number | null }

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

export function guardAction({ method, headers, contentLength }: GuardInput): GuardResult {
  if (method !== 'POST') return { ok: true }
  const host = headers.host ?? ''
  if (!host || headers.origin !== 'http://' + host) return { ok: false, status: 403, reason: 'Cross-origin action rejected' }
  if (headers['sec-fetch-site'] !== 'same-origin') return { ok: false, status: 403, reason: 'Cross-site action rejected' }
  if (!headers['x-forge-action']) return { ok: false, status: 403, reason: 'Action token missing' }
  if (!((headers['content-type'] ?? '').split(';')[0] ?? '').trim().startsWith('application/json')) return { ok: false, status: 415, reason: 'Actions are JSON' }
  if (!(contentLength > 0 && contentLength <= MAX_ACTION_BODY)) return { ok: false, status: 413, reason: 'Action body too large' }
  return { ok: true }
}

export function upstreamHeaders(headers: Headers, method: string, upstreamHost: string): Headers {
  const out: Headers = { host: upstreamHost }
  for (const name of ['cookie', 'content-type', 'x-forge-action', 'accept']) if (headers[name]) out[name] = headers[name]
  if (method === 'POST') { out.origin = 'http://' + upstreamHost; out['sec-fetch-site'] = 'same-origin' }
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
