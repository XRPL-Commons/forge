// @vitest-environment node
//
// This file runs under Node's environment, not the project-wide happy-dom
// environment: happy-dom's `Headers` shim joins repeated `set-cookie` values
// into one comma-separated string on iteration, unlike the spec-compliant
// (and production-matching, since the Nitro middleware runs in real Node)
// behavior of keeping each `set-cookie` as its own entry. The
// `responseHeaders` test below depends on that real behavior.
import { describe, it, expect } from 'vitest'
import { PassThrough } from 'node:stream'
import type { IncomingMessage } from 'node:http'
import { allowedHosts, guardAction, guardHost, proxyPaths, upstreamHeaders, readUpstreamSecret, responseHeaders, readCapped, readRequestBody } from '../../server/utils/proxy'

const base = { method: 'POST', contentLength: 100, headers: {
  host: '100.111.151.38:8787', origin: 'http://100.111.151.38:8787', 'sec-fetch-site': 'same-origin',
  'content-type': 'application/json', 'x-forge-action': 'tok' } }

describe('guardAction', () => {
  it('accepts a same-origin JSON action with the header', () => expect(guardAction(base)).toEqual({ ok: true }))
  it('lets GET through untouched', () => expect(guardAction({ ...base, method: 'GET', headers: {} })).toEqual({ ok: true }))
  it('rejects a foreign origin', () => expect(guardAction({ ...base, headers: { ...base.headers, origin: 'http://evil:8787' } })).toMatchObject({ ok: false, status: 403 }))
  it('accepts a POST without fetch metadata (plain-http origins never send Sec-Fetch-*)', () => {
    const { 'sec-fetch-site': _omit, ...headers } = base.headers
    expect(guardAction({ ...base, headers })).toEqual({ ok: true })
  })
  it('rejects cross-site fetch metadata', () => expect(guardAction({ ...base, headers: { ...base.headers, 'sec-fetch-site': 'cross-site' } })).toMatchObject({ ok: false, status: 403 }))
  it('rejects a missing action header', () => expect(guardAction({ ...base, headers: { ...base.headers, 'x-forge-action': '' } })).toMatchObject({ ok: false, status: 403 }))
  it('rejects non-JSON bodies', () => expect(guardAction({ ...base, headers: { ...base.headers, 'content-type': 'text/plain' } })).toMatchObject({ ok: false, status: 415 }))
  it('rejects oversized bodies', () => expect(guardAction({ ...base, contentLength: 262145 })).toMatchObject({ ok: false, status: 413 }))
})

describe('proxyPaths', () => {
  it('matches the API and the GitHub callback only', () => {
    expect(proxyPaths('/api/factory')).toBe(true)
    expect(proxyPaths('/api/issues?state=open')).toBe(true)
    expect(proxyPaths('/onboarding/github/callback')).toBe(true)
    expect(proxyPaths('/issues')).toBe(false)
    expect(proxyPaths('/favicon.svg')).toBe(false)
  })
  // @nuxt/icon mounts the bundled Lucide collection under /api/_nuxt_icon/;
  // forwarding it to Python would 404 every icon Nuxt UI draws itself.
  it('leaves the bundled icon route to Nitro', () => {
    expect(proxyPaths('/api/_nuxt_icon/lucide.json')).toBe(false)
    expect(proxyPaths('/api/_nuxt_icon/lucide.json?icons=check,chevron-down')).toBe(false)
    expect(proxyPaths('/api/_nuxt_icon')).toBe(false)
  })
})

// The DNS-rebinding defence: reaching this process is not enough, the name
// the browser used has to be one the app is served on.
describe('guardHost', () => {
  const allowed = allowedHosts({ HOST: '100.111.151.38', PORT: '8787' })
  it('accepts the exact configured address', () => expect(guardHost('100.111.151.38:8787', allowed)).toBe(true))
  it('rejects a rebound name on the same port', () => expect(guardHost('evil.example:8787', allowed)).toBe(false))
  it('rejects a missing Host header', () => {
    expect(guardHost(undefined, allowed)).toBe(false)
    expect(guardHost('', allowed)).toBe(false)
    expect(guardHost('   ', allowed)).toBe(false)
  })
  it('accepts loopback, so SSR-internal fetches and the e2e harness work', () => {
    expect(guardHost('127.0.0.1:8787', allowed)).toBe(true)
    expect(guardHost('localhost:8787', allowed)).toBe(true)
    expect(guardHost('localhost', allowed)).toBe(true)
    expect(guardHost('LOCALHOST:8787', allowed)).toBe(true)
  })
  it('rejects the right address on the wrong port', () => expect(guardHost('100.111.151.38:9999', allowed)).toBe(false))
})

describe('allowedHosts', () => {
  it('uses the e2e harness bind', () => {
    const set = allowedHosts({ HOST: '127.0.0.1', PORT: '3999' })
    expect(guardHost('127.0.0.1:3999', set)).toBe(true)
    expect(guardHost('evil.example:3999', set)).toBe(false)
  })
  it('falls back to the address Nitro reports when HOST/PORT are unset', () => {
    const set = allowedHosts({}, { address: '::ffff:100.111.151.38', port: 8787 })
    expect(guardHost('100.111.151.38:8787', set)).toBe(true)
  })
  it('never trusts a wildcard bind as a name', () => {
    const set = allowedHosts({ HOST: '0.0.0.0', PORT: '8787' })
    expect(guardHost('0.0.0.0:8787', set)).toBe(false)
    expect(guardHost('127.0.0.1:8787', set)).toBe(true)
  })
  it('is replaced wholesale by NITRO_ALLOWED_HOSTS', () => {
    const set = allowedHosts({ HOST: '100.111.151.38', PORT: '8787', NITRO_ALLOWED_HOSTS: 'forge.example:8787, forge.tail.ts.net' })
    expect(set).toEqual(['forge.example:8787', 'forge.tail.ts.net'])
    expect(guardHost('forge.tail.ts.net', set)).toBe(true)
    expect(guardHost('100.111.151.38:8787', set)).toBe(false)
  })
})

describe('upstreamHeaders', () => {
  it('rewrites host and origin and forwards cookies and the action header', () => {
    const out = upstreamHeaders(base.headers, 'POST', '127.0.0.1:8788')
    expect(out.host).toBe('127.0.0.1:8788')
    expect(out.origin).toBe('http://127.0.0.1:8788')
    expect(out['sec-fetch-site']).toBe('same-origin')
    expect(out['x-forge-action']).toBe('tok')
    expect(out['content-type']).toBe('application/json')
  })
  it('sends the upstream secret only when one is configured', () => {
    expect(upstreamHeaders({ host: 'x' }, 'GET', '127.0.0.1:8788', 's'.repeat(43))['x-forge-upstream']).toBe('s'.repeat(43))
    expect(upstreamHeaders({ host: 'x' }, 'GET', '127.0.0.1:8788')['x-forge-upstream']).toBeUndefined()
  })
  it('reads the secret file per request and tolerates a missing one', () => {
    expect(readUpstreamSecret('', () => 'unused')).toBe('')
    expect(readUpstreamSecret('/f', () => 'value\n')).toBe('value')
    expect(readUpstreamSecret('/f', () => { throw new Error('missing') })).toBe('')
  })
  it('forwards the validated browser host so App callbacks return through the UI', () => {
    const out = upstreamHeaders({ host: 'forge.tail.ts.net:8787' }, 'GET', '127.0.0.1:8788')
    expect(out['x-forwarded-host']).toBe('forge.tail.ts.net:8787')
    expect(out.host).toBe('127.0.0.1:8788')
  })
  it('does not invent an origin on GET', () => {
    const out = upstreamHeaders({ host: 'x', cookie: 'forge_setup=abc' }, 'GET', '127.0.0.1:8788')
    expect(out.origin).toBeUndefined(); expect(out.cookie).toBe('forge_setup=abc')
  })
})

describe('responseHeaders', () => {
  it('passes through only the allowed headers, keeping both set-cookie values in order', () => {
    const headers = new Headers()
    headers.append('set-cookie', 'a=1')
    headers.append('set-cookie', 'b=2')
    headers.set('content-type', 'application/json')
    // The generated ruleset is a download: without this header the browser
    // names the file after the route instead of forge-review-rules-<id>.json.
    headers.set('content-disposition', 'attachment; filename="forge-review-rules-2.json"')
    headers.set('location', '/next')
    headers.set('cache-control', 'no-store')
    // The upstream's own hardening headers survive the proxy hop.
    headers.set('referrer-policy', 'no-referrer')
    headers.set('x-content-type-options', 'nosniff')
    headers.set('x-secret', 'nope')
    const out = responseHeaders(headers)
    expect(out).toEqual([
      ['cache-control', 'no-store'],
      ['content-disposition', 'attachment; filename="forge-review-rules-2.json"'],
      ['content-type', 'application/json'],
      ['location', '/next'],
      ['referrer-policy', 'no-referrer'],
      ['set-cookie', 'a=1'],
      ['set-cookie', 'b=2'],
      ['x-content-type-options', 'nosniff'],
    ])
    expect(out.some(([name]) => name === 'x-secret')).toBe(false)
  })
})

function chunkStream(sizes: number[]): ReadableStream<Uint8Array> {
  return new ReadableStream<Uint8Array>({
    start(ctrl) {
      for (const size of sizes) ctrl.enqueue(new Uint8Array(size))
      ctrl.close()
    },
  })
}

describe('readCapped', () => {
  it('releases the reader instead of leaking it when the stream errors mid-read', async () => {
    // The underlying source enqueues one chunk on the first `pull`, then
    // throws on the second, which errors the stream.
    let pullCount = 0
    const stream = new ReadableStream<Uint8Array>({
      pull(ctrl) {
        pullCount += 1
        if (pullCount === 1) { ctrl.enqueue(new Uint8Array(4)); return }
        throw new Error('boom')
      },
    })
    const response = new Response(stream)
    await expect(readCapped(response, 1024, new AbortController())).rejects.toThrow('boom')
    // Per the WHATWG Streams spec, once a stream has errored, cancelling its
    // reader is a rejected no-op and does NOT invoke the underlying source's
    // own `cancel()` — verified directly against Node's ReadableStream before
    // writing this test, so a flag-in-`cancel()` assertion here would always
    // read false regardless of whether readCapped calls reader.cancel(). What
    // IS observable, and is the actual leak this fix closes, is whether the
    // reader's lock gets released: readCapped's `finally` must call
    // `reader.releaseLock()` so the stream isn't left locked forever.
    expect(response.body?.locked).toBe(false)
    expect(() => response.body?.getReader()).not.toThrow()
  })

  it('resolves to a Buffer of exactly that length when the total lands exactly on the limit', async () => {
    const response = new Response(chunkStream([10, 10, 10]))
    const result = await readCapped(response, 30, new AbortController())
    expect(result).toBeInstanceOf(Buffer)
    expect((result as Buffer).length).toBe(30)
  })

  it('returns the too-large sentinel one byte over the limit', async () => {
    const response = new Response(chunkStream([10, 10, 11]))
    const result = await readCapped(response, 30, new AbortController())
    expect(result).toEqual({ tooLarge: true })
  })
})

describe('operator HTTPS origin', () => {
  const secure = { ...base, publicOrigin: 'https://forge.example', headers: { ...base.headers, host: 'forge.example', origin: 'https://forge.example' } }
  it('accepts the explicitly configured HTTPS origin', () => expect(guardAction(secure)).toEqual({ ok: true }))
  it('rejects HTTP downgrade and spoofed forwarded protocol', () => {
    expect(guardAction({ ...secure, headers: { ...secure.headers, origin: 'http://forge.example', 'x-forwarded-proto': 'https' } })).toMatchObject({ ok: false })
  })
  it('requires the configured host as well as origin', () => {
    expect(guardAction({ ...secure, headers: { ...secure.headers, host: 'evil.example' } })).toMatchObject({ ok: false })
  })
  it('forwards both sign-in endpoints to the authenticated server', () => {
    expect(proxyPaths('/auth/github/start')).toBe(true)
    expect(proxyPaths('/auth/github/callback?code=abc&state=def')).toBe(true)
    expect(proxyPaths('/login')).toBe(false)
  })
})

describe('local client routes', () => {
  it('only bypasses browser CSRF for a fixed client endpoint with no ambient browser credentials', async () => {
    const { clientPath, guardClient } = await import('../../server/utils/proxy')
    expect(clientPath('/api/client/device')).toBe(true)
    for (const operation of ['authorize', 'read', 'operation', 'policy']) expect(clientPath(`/api/client/${operation}`)).toBe(true)
    expect(clientPath('/api/client/reset')).toBe(false)
    expect(clientPath('/api/client/device?bypass=1')).toBe(false)
    expect(clientPath('/api/settings')).toBe(false)
    const input = { method: 'POST', contentLength: 42, headers: { 'content-type': 'application/json' } }
    expect(guardClient(input)).toEqual({ ok: true })
    expect(guardClient({ ...input, method: 'GET' })).toMatchObject({ status: 405 })
    expect(guardClient({ ...input, headers: { ...input.headers, cookie: 'forge_session=token' } })).toMatchObject({ status: 403 })
    expect(guardClient({ ...input, headers: { ...input.headers, origin: 'https://forge.example' } })).toMatchObject({ status: 403 })
    expect(guardClient({ ...input, contentLength: 16385 })).toMatchObject({ status: 413 })
  })
  it('forwards a client bearer only on an explicit client route', () => {
    const headers = { host: 'forge.example', authorization: 'Bearer synthetic' }
    expect(upstreamHeaders(headers, 'POST', '127.0.0.1:8788', 'proxy', true)).toEqual({
      host: '127.0.0.1:8788', 'x-forge-upstream': 'proxy', 'x-forwarded-host': 'forge.example', authorization: 'Bearer synthetic' })
    expect(upstreamHeaders(headers, 'POST', '127.0.0.1:8788', 'proxy').authorization).toBeUndefined()
  })
})


describe('mutation request identity', () => {
  it('forwards a caller request identity and returns the server receipt header', () => {
    const identifier = 'a75b2d4e-6861-44f1-a9cc-6c44ba21092d'
    expect(upstreamHeaders({ 'x-forge-request-id': identifier }, 'POST', '127.0.0.1:8788')['x-forge-request-id']).toBe(identifier)
    expect(responseHeaders(new Headers({ 'x-forge-request-id': identifier }))).toContainEqual(['x-forge-request-id', identifier])
  })
})


describe('bounded request bodies', () => {
  const stream = () => new PassThrough() as unknown as IncomingMessage
  it('accepts a missing content length while still enforcing a streamed bound', async () => {
    expect(guardAction({ ...base, contentLength: undefined })).toEqual({ ok: true })
    const request = stream()
    const result = readRequestBody(request, 8, new AbortController().signal)
    request.emit('data', Buffer.from('{')); request.emit('data', Buffer.from('}')); request.emit('end')
    expect((await result).toString()).toBe('{}')
    expect(request.listenerCount('data')).toBe(0)
  })
  it('rejects a chunked body as soon as it exceeds the bound', async () => {
    const request = stream()
    const result = readRequestBody(request, 2, new AbortController().signal)
    request.emit('data', Buffer.from('123'))
    await expect(result).rejects.toMatchObject({ status: 413 })
    expect(request.listenerCount('data')).toBe(0)
  })
  it('returns 400 for declared and streamed empty bodies', async () => {
    expect(guardAction({ ...base, contentLength: 0 })).toMatchObject({ status: 400 })
    const request = stream()
    const result = readRequestBody(request, 8, new AbortController().signal)
    request.emit('end')
    await expect(result).rejects.toMatchObject({ status: 400 })
  })
  it('handles aborted and slow bodies before any upstream fetch', async () => {
    const request = stream()
    const result = readRequestBody(request, 8, new AbortController().signal)
    request.emit('aborted')
    await expect(result).rejects.toMatchObject({ status: 400 })
    const slow = stream(); const controller = new AbortController()
    const waiting = readRequestBody(slow, 8, controller.signal)
    controller.abort()
    await expect(waiting).rejects.toMatchObject({ status: 408 })
    expect(slow.listenerCount('data')).toBe(0)
  })
})
