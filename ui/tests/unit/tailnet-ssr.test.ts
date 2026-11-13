// @vitest-environment node
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, createEvent, defineEventHandler, fetchWithEvent, toNodeListener } from 'h3'
import { IncomingMessage, ServerResponse, fetchNodeRequestHandler } from 'node-mock-http'
import { createFetch } from 'ofetch'
import { requestTailscaleIdentity, upstreamHeaders } from '../../server/utils/proxy'
const procTable = vi.hoisted(() => [
  '  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode',
  // Serve's root-owned loopback connection from port 40000 to the UI on 8787.
  '   0: 0100007F:9C40 0100007F:2253 01 00000000:00000000 00:00000000 00000000     0        0 13 1',
].join('\n'))
vi.mock('node:fs', async (original) => {
  const fs = await original<typeof import('node:fs')>()
  const readFileSync = ((path: unknown, ...rest: unknown[]) => path === '/proc/net/tcp' ? procTable : (fs.readFileSync as any)(path, ...rest)) as typeof fs.readFileSync
  return { ...fs, default: { ...fs, readFileSync }, readFileSync }
})

const origin = 'anvil.example.ts.net'
const env = { NITRO_FORGE_TAILSCALE_IDENTITY: 'serve', NITRO_FORGE_PUBLIC_ORIGIN: 'https://' + origin }
function outer(remoteAddress = '127.0.0.1', login = 'operator@example.com') {
  const req = new IncomingMessage()
  req.url = '/'; req.headers = { host: origin, 'tailscale-user-login': login }
  Object.assign(req.socket, { remoteAddress, remotePort: 40000, localAddress: '127.0.0.1', localPort: 8787 })
  return createEvent(req as any, new ServerResponse(req) as any)
}
afterEach(() => vi.unstubAllEnvs())

describe('Tailscale identity across server rendering', () => {
  it('retains verified page identity through Nitro internal fetch without an operator cookie', async () => {
    const page = outer()
    expect(requestTailscaleIdentity(page, env)).toBe('operator@example.com')
    const calls: Array<{ path: string; login: string | undefined; socket: string | undefined }> = []
    const app = createApp()
    app.use(defineEventHandler(event => {
      const login = requestTailscaleIdentity(event, env)
      calls.push({ path: event.path, login, socket: event.node.req.socket?.remoteAddress })
      const forwarded = upstreamHeaders(event.node.req.headers as Record<string, string>, 'GET', 'localhost:8788', 'private', false, login)
      return { ok: true, operator: forwarded['x-forge-tailscale-user-login'] ? { id: 1000 } : null }
    }))
    // This is Nitro's localFetch adapter, used by useRequestFetch during SSR.
    const localFetch = (path: string, init: any) => fetchNodeRequestHandler(toNodeListener(app), path, init)
    for (const path of ['/api/auth/session', '/api/onboarding']) {
      const response = await fetchWithEvent(page, path, {}, { fetch: localFetch as typeof fetch })
      expect((await response.json()).operator).toEqual({ id: 1000 })
    }
    // useForgeApi retains raw response access for ETags and timing headers.
    const request = createFetch({ fetch: localFetch as typeof fetch })
    const response = await request.raw('/api/issues', { headers: { host: origin }, ...{ context: page.context } })
    expect(response._data.operator).toEqual({ id: 1000 })
    expect(calls).toHaveLength(3)
    expect(calls.every(row => !row.socket && row.login === 'operator@example.com')).toBe(true)
    expect(page.node.req.headers.cookie).toBeUndefined()
  })

  it('does not turn raw headers or arbitrary internal contexts into trusted identity', () => {
    for (const address of ['', '100.64.0.2']) {
      const page = outer(address)
      page.node.req.headers['x-forge-tailscale-user-login'] = 'operator@example.com'
      expect(requestTailscaleIdentity(page, env)).toBeUndefined()
      const req = new IncomingMessage()
      req.headers = { host: origin, 'tailscale-user-login': 'operator@example.com' }
      req.__unenv__ = page.context
      expect(requestTailscaleIdentity(createEvent(req as any, new ServerResponse(req) as any), env)).toBeUndefined()
    }
    expect(requestTailscaleIdentity(outer('127.0.0.1'), {})).toBeUndefined()
    expect(requestTailscaleIdentity(outer('127.0.0.1', 'tagged-devices'), env)).toBeUndefined()
  })

  it('isolates page requests and continues stripping identity from agent routes', () => {
    const page = outer()
    const login = requestTailscaleIdentity(page, env)
    expect(upstreamHeaders({ host: origin }, 'POST', 'localhost:8788', 'private', true, login))
      .not.toHaveProperty('x-forge-tailscale-user-login')
    expect(requestTailscaleIdentity(outer('100.64.0.2'), env)).toBeUndefined()
  })
})
