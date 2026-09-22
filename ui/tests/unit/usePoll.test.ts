import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { usePoll } from '../../app/composables/usePoll'

describe('usePoll', () => {
  beforeEach(() => { vi.useFakeTimers(); Object.defineProperty(document, 'hidden', { value: false, configurable: true }) })
  afterEach(() => vi.useRealTimers())

  it('coalesces overlapping refreshes and keeps the last payload on failure', async () => {
    let calls = 0
    const load = vi.fn(async () => { calls++; if (calls === 2) throw new Error('down'); return { n: calls } })
    const poll = usePoll(load, { interval: 5000 })
    const a = poll.refresh(); const b = poll.refresh(); await Promise.all([a, b])
    expect(load).toHaveBeenCalledTimes(1); expect(poll.data.value).toEqual({ n: 1 }); expect(poll.stale.value).toBe(false)
    await poll.refresh()
    expect(poll.data.value).toEqual({ n: 1 }); expect(poll.stale.value).toBe(true); expect(poll.error.value).toBe('down')
  })

  it('backs off to 15 s after a failure and resumes 5 s on success', async () => {
    let fail = true
    const load = vi.fn(async () => { if (fail) throw new Error('x'); return 1 })
    const poll = usePoll(load, { interval: 5000 }); poll.start(); await vi.advanceTimersByTimeAsync(0)
    expect(load).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(5000); expect(load).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(10000); expect(load).toHaveBeenCalledTimes(2)
    fail = false; await vi.advanceTimersByTimeAsync(15000); expect(load).toHaveBeenCalledTimes(3)
    await vi.advanceTimersByTimeAsync(5000); expect(load).toHaveBeenCalledTimes(4)
    poll.stop()
  })

  it('does not poll while the tab is hidden and refreshes when it becomes visible', async () => {
    Object.defineProperty(document, 'hidden', { value: true, configurable: true })
    const load = vi.fn(async () => 1)
    const poll = usePoll(load, { interval: 5000 }); poll.start(); await vi.advanceTimersByTimeAsync(20000)
    expect(load).not.toHaveBeenCalled()
    Object.defineProperty(document, 'hidden', { value: false, configurable: true })
    document.dispatchEvent(new Event('visibilitychange')); await vi.advanceTimersByTimeAsync(0)
    expect(load).toHaveBeenCalledTimes(1); poll.stop()
  })

  it('calls onChange only when the payload changed', async () => {
    const onChange = vi.fn(); const load = vi.fn(async () => ({ same: true }))
    const poll = usePoll(load, { onChange }); await poll.refresh(); await poll.refresh()
    expect(onChange).toHaveBeenCalledTimes(1)
  })

  it('skips the load while allowed() is false and leaves data, error and stale untouched', async () => {
    let allowed = false
    const load = vi.fn(async () => ({ n: 1 }))
    const poll = usePoll(load, { interval: 5000, allowed: () => allowed })
    poll.start(); await vi.advanceTimersByTimeAsync(0)
    expect(load).not.toHaveBeenCalled()
    expect(poll.data.value).toBe(null); expect(poll.error.value).toBe(null); expect(poll.stale.value).toBe(false)
    await vi.advanceTimersByTimeAsync(5000)
    expect(load).not.toHaveBeenCalled()
    allowed = true
    await vi.advanceTimersByTimeAsync(5000)
    expect(load).toHaveBeenCalledTimes(1)
    poll.stop()
  })
})
