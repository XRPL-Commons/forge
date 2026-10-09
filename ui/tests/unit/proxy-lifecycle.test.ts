// @vitest-environment node
import { afterEach, describe, expect, it, vi } from 'vitest'
import { EventEmitter } from 'node:events'
import { PassThrough } from 'node:stream'
vi.mock('#app/nuxt', () => ({ useRuntimeConfig: () => ({ forgeApi: 'http://localhost:8788', forgeUpstreamSecretFile: '' }) }))
vi.mock('#app/composables/ssr', () => ({ setResponseStatus: vi.fn() }))

async function middleware() {
  vi.stubGlobal('defineEventHandler', (handler: unknown) => handler)
  vi.stubGlobal('useRuntimeConfig', () => ({ forgeApi: 'http://localhost:8788', forgeUpstreamSecretFile: '' }))
  for (const name of ['setResponseStatus', 'setResponseHeader', 'appendResponseHeader']) vi.stubGlobal(name, vi.fn())
  return (await import('../../server/middleware/forge-proxy')).default
}
function request(method = 'GET') {
  const req = Object.assign(new PassThrough(), { method, headers: { host: 'localhost' }, socket: {} })
  return { path: '/api/factory/changes?since=instance%3A1&wait=25', node: { req, res: new EventEmitter() } }
}
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers() })

describe('proxy request lifetime', () => {
  it('cancels the upstream long poll when its viewer disconnects', async () => {
    const handler = await middleware(), event = request()
    let signal: AbortSignal | undefined
    vi.stubGlobal('fetch', vi.fn((_path, options) => {
      signal = options.signal
      return new Promise((_resolve, reject) => signal!.addEventListener('abort', () => reject(new Error('closed'))))
    }))
    const pending = handler(event as any)
    expect(signal?.aborted).toBe(false)
    event.node.res.emit('close')
    await pending
    expect(signal?.aborted).toBe(true)
    expect(event.node.res.listenerCount('close')).toBe(0)
  })
  it('removes its deadline and disconnect listener after a normal response', async () => {
    vi.useFakeTimers()
    const handler = await middleware(), event = request()
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { headers: { 'content-type': 'application/json' } })))
    expect(await handler(event as any)).toEqual(Buffer.from('{}'))
    expect(event.node.res.listenerCount('close')).toBe(0)
    expect(vi.getTimerCount()).toBe(0)
  })
  it('closes an unread rejected upload instead of retaining the client connection', async () => {
    const handler = await middleware(), event = request('POST')
    vi.stubGlobal('fetch', vi.fn())
    await handler(event as any)
    expect(fetch).not.toHaveBeenCalled()
    expect(setResponseHeader).toHaveBeenCalledWith(event, 'connection', 'close')
  })
})
