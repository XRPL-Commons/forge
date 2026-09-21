export const MAX_ACTION_BODY = 262144
export const MAX_RESPONSE_BODY = 1024 * 1024
export const UPSTREAM_TIMEOUT_MS = 30000
const PASS_RESPONSE = ['content-type', 'set-cookie', 'location', 'cache-control']

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
