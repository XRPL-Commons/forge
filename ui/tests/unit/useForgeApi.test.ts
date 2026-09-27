import { describe, it, expect, vi, beforeEach } from 'vitest'

// This project auto-imports `useState` and `$fetch` (Nuxt/unimport rewrites the bare
// identifiers in app/** source into real imports from '#app/composables/state' and
// '#build/fetch.mjs' at transform time — see ui/.nuxt/imports.d.ts). A plain
// `vi.stubGlobal('useState', ...)` never runs because the composable no longer reads
// those identifiers off globalThis after the rewrite; the mocks below target the
// modules the rewrite actually resolves to instead. `vi.hoisted` is required so the
// mock factories can close over fresh fakes despite `vi.mock` calls being hoisted
// above normal `const`/`import` statements.
const { store, fetchRaw } = vi.hoisted(() => ({
  store: {} as Record<string, { value: any }>,
  fetchRaw: vi.fn(),
}))

vi.mock('#app/composables/state', () => ({
  useState: (key: string, init: () => any) => { store[key] ??= { value: init() }; return store[key] },
}))
vi.mock('../../app/composables/useResource', () => ({ useResources: () => ({ invalidate: vi.fn() }) }))
vi.mock('#build/fetch.mjs', () => ({
  $fetch: Object.assign(vi.fn(), { raw: fetchRaw }),
}))

const { useForgeApi, ForgeApiError } = await import('../../app/composables/useForgeApi')

beforeEach(() => {
  for (const key of Object.keys(store)) delete store[key]
  fetchRaw.mockReset()
})

describe('useForgeApi', () => {
  it('get returns the payload and stores controls_token', async () => {
    fetchRaw.mockResolvedValue({ status: 200, ok: true, _data: { ok: true, controls_token: 'tok-1', value: 42 } })
    const api = useForgeApi()
    const payload = await api.get('/api/status')
    expect(payload).toEqual({ ok: true, controls_token: 'tok-1', value: 42 })
    expect(api.token.value).toBe('tok-1')
  })

  it('get on a 200 {ok:false} response throws ForgeApiError with status and reason', async () => {
    fetchRaw.mockResolvedValue({ status: 200, ok: true, _data: { ok: false, reason: 'nope' } })
    const api = useForgeApi()
    await expect(api.get('/api/status')).rejects.toBeInstanceOf(ForgeApiError)
    await expect(api.get('/api/status')).rejects.toMatchObject({ status: 200, message: 'nope' })
  })

  it('get normalizes a thrown FetchError to ForgeApiError', async () => {
    fetchRaw.mockRejectedValue({ response: { status: 503, _data: { reason: 'down' } } })
    const api = useForgeApi()
    await expect(api.get('/api/status')).rejects.toBeInstanceOf(ForgeApiError)
    await expect(api.get('/api/status')).rejects.toMatchObject({ status: 503, message: 'down' })
  })

  it('act sends X-Forge-Action equal to the stored token and Content-Type json', async () => {
    fetchRaw
      .mockResolvedValueOnce({ status: 200, ok: true, _data: { ok: true, controls_token: 'secret-token' } })
      .mockResolvedValueOnce({ status: 200, ok: true, _data: { ok: true } })
    const api = useForgeApi()
    await api.get('/api/status')
    await api.act('/api/deploy', { thing: 1 })
    const call = fetchRaw.mock.calls[1]
    expect(call[0]).toBe('/api/deploy')
    expect(call[1].headers).toEqual({ 'Content-Type': 'application/json', 'X-Forge-Action': 'secret-token' })
  })

  it('act on a 403 rejection with no reason throws the out-of-date message', async () => {
    fetchRaw.mockRejectedValue({ response: { status: 403, _data: {} } })
    const api = useForgeApi()
    await expect(api.act('/api/deploy', {})).rejects.toMatchObject({ status: 403, message: 'This page is out of date. Refresh and try again.' })
  })

  it('act on a rejection with no response throws the generic message', async () => {
    fetchRaw.mockRejectedValue(new Error('network down'))
    const api = useForgeApi()
    await expect(api.act('/api/deploy', {})).rejects.toMatchObject({ status: 0, message: 'Forge did not accept the action.' })
  })

  it('get defaults to a 15 s abort signal', async () => {
    fetchRaw.mockResolvedValue({ status: 200, ok: true, _data: { ok: true } })
    const spy = vi.spyOn(AbortSignal, 'timeout')
    const api = useForgeApi()
    await api.get('/api/factory')
    expect(spy).toHaveBeenCalledWith(15000)
    expect((fetchRaw.mock.calls[0] as any)[1].signal).toBeInstanceOf(AbortSignal)
    spy.mockRestore()
  })

  it('get honours an explicit timeout', async () => {
    fetchRaw.mockResolvedValue({ status: 200, ok: true, _data: { ok: true } })
    const spy = vi.spyOn(AbortSignal, 'timeout')
    const api = useForgeApi()
    await api.get('/api/issues?state=open', { timeout: 30000 })
    expect(spy).toHaveBeenCalledWith(30000)
    spy.mockRestore()
  })
})
