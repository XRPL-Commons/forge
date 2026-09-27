import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { effectScope, ref, nextTick } from 'vue'
const nuxt = vi.hoisted(() => ({ app: {} }))
vi.mock('#app/nuxt', () => ({ useNuxtApp: () => nuxt.app, useRuntimeConfig: () => ({ public: { pollInterval: 5000 } }) }))
const { useResource, useResources } = await import('../../app/composables/useResource')

describe('resource subscriptions', () => {
  beforeEach(() => { nuxt.app = {}; vi.useFakeTimers(); vi.setSystemTime(10000); Object.defineProperty(document, 'hidden', { value: false, configurable: true }) })
  afterEach(() => vi.useRealTimers())
  it('shares one poll and hands ownership over on disposal', async () => {
    const a = effectScope(), b = effectScope(), loader = vi.fn(async () => ({ count: 1 }))
    const first = a.run(() => useResource('/factory', loader, { interval: 5000 }))!
    const second = b.run(() => useResource('/factory', loader, { interval: 5000 }))!
    first.start(); second.start(); await vi.advanceTimersByTimeAsync(0)
    expect(loader).toHaveBeenCalledTimes(1); expect(vi.getTimerCount()).toBe(1)
    await vi.advanceTimersByTimeAsync(5000); expect(loader).toHaveBeenCalledTimes(2)
    a.stop(); await vi.advanceTimersByTimeAsync(0)
    expect(vi.getTimerCount()).toBe(1)
    b.stop(); expect(vi.getTimerCount()).toBe(0)
  })
  it('switches query keys without displaying the late old response', async () => {
    const scope = effectScope(), key = ref('/issues?a'), resolvers = new Map<string, (value: string) => void>()
    const resource = scope.run(() => useResource(key, k => new Promise<string>(r => resolvers.set(k, r)), { interval: 5000 }))!
    resource.start(); await vi.advanceTimersByTimeAsync(0)
    key.value = '/issues?b'; await nextTick(); await vi.advanceTimersByTimeAsync(0)
    resolvers.get('/issues?b')!('new'); await vi.advanceTimersByTimeAsync(0)
    resolvers.get('/issues?a')!('old'); await vi.advanceTimersByTimeAsync(0)
    expect(resource.data.value).toBe('new'); scope.stop()
  })
  it('renders warm navigation without another GET and pauses hidden polling', async () => {
    const loader = vi.fn(async () => 42), firstScope = effectScope()
    const first = firstScope.run(() => useResource('/issues', loader, { interval: 5000 }))!
    first.start(); await vi.advanceTimersByTimeAsync(0); firstScope.stop()
    const secondScope = effectScope()
    const second = secondScope.run(() => useResource('/issues', loader, { interval: 5000 }))!
    expect(second.data.value).toBe(42)
    second.start(); await vi.advanceTimersByTimeAsync(0); expect(loader).toHaveBeenCalledTimes(1)
    Object.defineProperty(document, 'hidden', { value: true, configurable: true }); document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(20000); expect(loader).toHaveBeenCalledTimes(1)
    secondScope.stop()
  })
})
