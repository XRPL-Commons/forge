// @vitest-environment nuxt
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mountSuspended, mockNuxtImport } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import { ref } from 'vue'
import { useRuntimeConfig } from '#imports'
import Login from '../../../app/pages/login.vue'

const { refresh, operator, navigate, route } = vi.hoisted(() => ({
  refresh: vi.fn(), operator: vi.fn().mockReturnValue({ session: { value: { ok: true, enabled: false, operator: null } }, checked: { value: Date.now() } }), navigate: vi.fn(),
  route: { query: {} as Record<string, unknown> },
}))
mockNuxtImport('useOperator', () => operator)
mockNuxtImport('useRoute', () => () => route)
mockNuxtImport('navigateTo', () => navigate)
mockNuxtImport('useFactoryIdentity', () => () => ({ name: 'Anvil', named: true }))

describe('login setup readiness', () => {
  let session: ReturnType<typeof ref<any>>
  let wrapper: Awaited<ReturnType<typeof mountSuspended>> | undefined
  beforeEach(() => {
    useRuntimeConfig().public.forgeSetupPending = true
    route.query = {}
    navigate.mockReset(); refresh.mockReset()
    session = ref({ ok: true, enabled: false, operator: null })
    operator.mockReturnValue({ session, checked: ref(Date.now()), refresh })
    refresh.mockImplementation(async () => session.value)
    Object.defineProperty(document, 'hidden', { configurable: true, value: false })
  })
  afterEach(() => { wrapper?.unmount(); wrapper = undefined; vi.useRealTimers() })
  async function mount() {
    wrapper = await mountSuspended(Login, { global: { stubs: { SignInScene: true } } })
    await flushPromises()
    return wrapper
  }
  function respond(value: any) { refresh.mockImplementation(async () => { session.value = value; return value }) }

  it('automatically leaves an already-open setup screen when Tailscale signs the operator in', async () => {
    const page = await mount()
    expect(page.text()).toContain('Your factory is getting ready')
    expect(refresh).toHaveBeenCalledTimes(1)
    vi.useFakeTimers()
    // Reschedule under the fake clock, then let the regular interval discover
    // readiness without a click, reload or another visibility event.
    document.dispatchEvent(new Event('visibilitychange')); await flushPromises()
    respond({ ok: true, enabled: true, operator: { id: 1, login: 'operator' } })
    await vi.advanceTimersByTimeAsync(5000); await flushPromises()
    expect(navigate).toHaveBeenCalledWith('/installation', { replace: true })
    const count = refresh.mock.calls.length
    await vi.advanceTimersByTimeAsync(30_000)
    expect(refresh).toHaveBeenCalledTimes(count)
  })

  it('shows GitHub sign-in when ready and preserves an allowed return path', async () => {
    route.query = { return_to: '/settings/agents?code=ABCD-EFGH' }
    const page = await mount()
    respond({ ok: true, enabled: true, operator: null })
    document.dispatchEvent(new Event('visibilitychange')); await flushPromises()
    expect(page.text()).not.toContain('Your factory is getting ready')
    expect(page.get('a').attributes('href')).toBe('/auth/github/start?return_to=%2Fsettings%2Fagents%3Fcode%3DABCD-EFGH')
    expect(navigate).not.toHaveBeenCalled()
  })

  it('waits while hidden, survives a failed check, and cannot redirect after unmount', async () => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: true })
    await mount()
    expect(refresh).not.toHaveBeenCalled()
    Object.defineProperty(document, 'hidden', { configurable: true, value: false })
    refresh.mockRejectedValueOnce(new Error('offline'))
    document.dispatchEvent(new Event('visibilitychange')); await flushPromises()
    expect(wrapper!.text()).toContain('Your factory is getting ready')
    let resolve!: (value: any) => void
    refresh.mockImplementation(() => new Promise(done => { resolve = done }))
    document.dispatchEvent(new Event('visibilitychange')); await flushPromises()
    wrapper!.unmount(); wrapper = undefined
    resolve({ ok: true, enabled: true, operator: { id: 1 } }); await flushPromises()
    expect(navigate).not.toHaveBeenCalled()
    const count = refresh.mock.calls.length
    document.dispatchEvent(new Event('visibilitychange')); await flushPromises()
    expect(refresh).toHaveBeenCalledTimes(count)
  })

  it('does not poll ordinary sign-in pages', async () => {
    useRuntimeConfig().public.forgeSetupPending = false
    session.value = { ok: true, enabled: true, operator: null }
    await mount()
    document.dispatchEvent(new Event('visibilitychange')); await flushPromises()
    expect(refresh).not.toHaveBeenCalled()
  })
})
