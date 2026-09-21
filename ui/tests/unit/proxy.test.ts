// @vitest-environment node
//
// This file runs under Node's environment, not the project-wide happy-dom
// environment: happy-dom's `Headers` shim joins repeated `set-cookie` values
// into one comma-separated string on iteration, unlike the spec-compliant
// (and production-matching, since the Nitro middleware runs in real Node)
// behavior of keeping each `set-cookie` as its own entry. The
// `responseHeaders` test below depends on that real behavior.
import { describe, it, expect } from 'vitest'
import { guardAction, proxyPaths, upstreamHeaders, responseHeaders, readCapped } from '../../server/utils/proxy'

const base = { method: 'POST', contentLength: 100, headers: {
  host: '100.111.151.38:8787', origin: 'http://100.111.151.38:8787', 'sec-fetch-site': 'same-origin',
  'content-type': 'application/json', 'x-forge-action': 'tok' } }

describe('guardAction', () => {
  it('accepts a same-origin JSON action with the header', () => expect(guardAction(base)).toEqual({ ok: true }))
  it('lets GET through untouched', () => expect(guardAction({ ...base, method: 'GET', headers: {} })).toEqual({ ok: true }))
  it('rejects a foreign origin', () => expect(guardAction({ ...base, headers: { ...base.headers, origin: 'http://evil:8787' } })).toMatchObject({ ok: false, status: 403 }))
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
    headers.set('location', '/next')
    headers.set('cache-control', 'no-store')
    headers.set('x-secret', 'nope')
    const out = responseHeaders(headers)
    expect(out).toEqual([
      ['cache-control', 'no-store'],
      ['content-type', 'application/json'],
      ['location', '/next'],
      ['set-cookie', 'a=1'],
      ['set-cookie', 'b=2'],
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
