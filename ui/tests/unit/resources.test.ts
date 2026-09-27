import { describe, it, expect, vi } from 'vitest'
import { Resources } from '../../app/utils/resources'

describe('shared resources', () => {
  it('deduplicates reads, returns warm data immediately and separates query keys', async () => {
    const cache = new Resources(() => 10000)
    let resolve!: (value: object) => void
    const load = vi.fn(() => new Promise<object>(r => { resolve = r }))
    const a = cache.load('/issues?page=1', load), b = cache.load('/issues?page=1', load)
    await Promise.resolve(); resolve({ count: 1 }); await Promise.all([a, b])
    expect(load).toHaveBeenCalledTimes(1)
    expect(await cache.load('/issues?page=1', load, 5000)).toEqual({ count: 1 })
    expect(load).toHaveBeenCalledTimes(1)
    expect(cache.entry('/issues?page=2').data.value).toBeNull()
  })
  it('discards a pre-action response and wakes subscribers for authoritative data', async () => {
    const cache = new Resources(() => 10000)
    let resolve!: (value: object) => void
    const wake = vi.fn()
    const entry = cache.entry('/factory'); entry.listeners.add(wake)
    cache.replace('/factory', { count: 1 })
    const read = cache.load('/factory', () => new Promise<object>(r => { resolve = r }))
    await Promise.resolve(); cache.invalidate(); cache.replace('/factory', { count: 2 })
    resolve({ count: 0 }); await read
    expect(entry.data.value).toEqual({ count: 2 }); expect(wake).toHaveBeenCalledTimes(2)
  })
  it('retains last known data on errors, expires freshness and preserves unchanged identity', async () => {
    let clock = 10000
    const cache = new Resources(() => clock)
    await cache.load('/factory', async () => ({ count: 1 }))
    const first = cache.entry('/factory').data.value
    clock += 6000
    const load = vi.fn(async () => ({ count: 1 }))
    await cache.load('/factory', load, 5000)
    expect(load).toHaveBeenCalledTimes(1); expect(cache.entry('/factory').data.value).toBe(first)
    await cache.load('/factory', async () => { throw new Error('offline') })
    expect(cache.entry('/factory').data.value).toBe(first)
    expect(cache.entry('/factory').error.value).toBe('offline')
  })
  it('clears private state on reset and cannot refill it from an older request', async () => {
    const cache = new Resources()
    cache.replace('/issue', { private: true })
    let resolve!: (value: object) => void
    const read = cache.load('/issue', () => new Promise<object>(r => { resolve = r }))
    await Promise.resolve(); cache.invalidate(() => true, true); resolve({ private: true }); await read
    expect(cache.entry('/issue').data.value).toBeNull()
  })
  it('bounds idle cache entries and never shares state across SSR requests', () => {
    const cache = new Resources(() => 10000, 2)
    cache.replace('/one', 1); cache.replace('/two', 2); cache.replace('/three', 3)
    expect(cache.entries.size).toBe(2)
    expect(new Resources().entry('/three').data.value).toBeNull()
  })
})
