import { describe, it, expect } from 'vitest'
import { guardAction, proxyPaths, upstreamHeaders } from '../../server/utils/proxy'

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
