export const MAX_ACTION_BODY = 262144
export const MAX_RESPONSE_BODY = 1024 * 1024
export const UPSTREAM_TIMEOUT_MS = 30000
// `content-disposition` carries the ruleset download's filename
// (`forge-review-rules-<id>.json`, contracts §2); dropping it renames the file.
const PASS_RESPONSE = ['content-type', 'content-disposition', 'set-cookie', 'location', 'cache-control']

type Headers = Record<string, string | undefined>
export interface GuardInput { method: string; headers: Headers; contentLength: number }
export type GuardResult = { ok: true } | { ok: false; status: number; reason: string }

export function proxyPaths(path: string): boolean {
  const clean = path.split('?')[0]
  return clean === '/api' || clean.startsWith('/api/') || clean === '/onboarding/github/callback'
}

export function guardAction({ method, headers, contentLength }: GuardInput): GuardResult {
  if (method !== 'POST') return { ok: true }
  const host = headers.host ?? ''
  if (!host || headers.origin !== 'http://' + host) return { ok: false, status: 403, reason: 'Cross-origin action rejected' }
  if (headers['sec-fetch-site'] !== 'same-origin') return { ok: false, status: 403, reason: 'Cross-site action rejected' }
  if (!headers['x-forge-action']) return { ok: false, status: 403, reason: 'Action token missing' }
  if (!(headers['content-type'] ?? '').split(';')[0].trim().startsWith('application/json')) return { ok: false, status: 415, reason: 'Actions are JSON' }
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
