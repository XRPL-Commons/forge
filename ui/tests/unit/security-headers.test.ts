// @vitest-environment node
import { afterEach, describe, expect, it, vi } from 'vitest'

afterEach(() => { vi.unstubAllGlobals() })

describe('security headers', () => {
  it('sends the page hardening headers, including framing protection', async () => {
    const setResponseHeader = vi.fn()
    vi.stubGlobal('defineEventHandler', (handler: unknown) => handler)
    vi.stubGlobal('setResponseHeader', setResponseHeader)
    const handler = (await import('../../server/middleware/security-headers')).default as (event: unknown) => void
    const event = {}
    handler(event)
    for (const [target] of setResponseHeader.mock.calls) expect(target).toBe(event)
    expect(Object.fromEntries(setResponseHeader.mock.calls.map(([, name, value]) => [name, value]))).toEqual({
      'Referrer-Policy': 'no-referrer',
      'X-Content-Type-Options': 'nosniff',
      'Content-Security-Policy': "frame-ancestors 'none'",
      'X-Frame-Options': 'DENY',
    })
  })
})
