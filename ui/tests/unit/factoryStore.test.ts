import { describe, it, expect, vi, afterEach } from 'vitest'
import { FactoryStore, BOARD_PATH, type FactoryChanges } from '../../app/utils/factoryStore'
import { Resources } from '../../app/utils/resources'
import type { FactoryView } from '../../app/utils/factory'

const board = (title = 'First'): FactoryView => ({ ok: true, cursor: 'instance:1', controls_token: 'csrf', active: true,
  operator: null, runtime: { enabled: true, slots: {}, cycle_seconds: 5 }, runtime_revision: '1',
  issues: [{ key: '1:1', title }], jobs: [{ id: 'job', issue_key: '1:1', phase: 'running' }],
} as FactoryView)
const batch = (value = board(), extras = {}): FactoryChanges => ({ ok: true, cursor: 'instance:2', events: [{ sequence: 2, issue_id: '1:1', unit_id: null, job_id: 'job', kind: 'stage_completed', at: 100 }], issues: { '1:1': { key: '1:1', title: value.issues![0]!.title } }, board: value, resync: false, ...extras } as FactoryChanges)
const tick = async () => { await vi.advanceTimersByTimeAsync(0) }
afterEach(() => vi.useRealTimers())

describe('shared factory feed', () => {
  it('preserves unchanged entity identity and refreshes only affected lazy detail', () => {
    const resources = new Resources()
    const store = new FactoryStore(resources, vi.fn())
    const changed = vi.fn(), untouched = vi.fn()
    resources.entry('/api/issues?issue=1%3A1').listeners.add(changed)
    resources.entry('/api/issues?issue=1%3A2').listeners.add(untouched)
    store.apply(batch())
    const first = resources.entry<FactoryView>(BOARD_PATH).data.value!
    const summary = store.issues.value.get('1:1')
    store.apply(batch())
    const next = resources.entry<FactoryView>(BOARD_PATH).data.value!
    expect(next.issues![0]).toBe(first.issues![0]); expect(next.jobs![0]).toBe(first.jobs![0])
    expect(store.issues.value.get('1:1')).toBe(summary)
    expect(changed).toHaveBeenCalledTimes(2); expect(untouched).not.toHaveBeenCalled()
    expect([...resources.entries.keys()].some(path => path.includes('output'))).toBe(false)
    store.apply(batch(board('Changed')))
    expect(resources.entry<FactoryView>(BOARD_PATH).data.value!.issues![0]).not.toBe(first.issues![0])
  })
  it('resync and idle health responses refresh mounted details without eagerly loading unopened bodies', () => {
    const resources = new Resources(), read = vi.fn(), store = new FactoryStore(resources, read)
    const wake = vi.fn(); resources.entry('/api/issues?issue=1%3A9').listeners.add(wake)
    store.apply(batch(board(), { resync: true, issues: {}, events: [] }))
    expect(wake).toHaveBeenCalledOnce(); expect(store.issues.value.size).toBe(0); expect(read).not.toHaveBeenCalled()
    store.apply(batch(board(), { events: [] }))
    expect(wake).toHaveBeenCalledTimes(2)
  })
  it('a reset fence replaces the visible board instead of leaving stale work running', async () => {
    vi.useFakeTimers()
    const resources = new Resources()
    const read = vi.fn(async path => {
      if (path === BOARD_PATH) return board()
      throw Object.assign(new Error('Setup is being reset.'), { status: 200 })
    })
    const store = new FactoryStore(resources, read as any)
    store.start(); await tick()
    expect(store.entry.data.value).toEqual({ ok: false, reason: 'Setup is being reset.' })
    store.stop()
  })
  it('shares one long poll across consumers and cancels it on the final stop', async () => {
    vi.useFakeTimers()
    const resources = new Resources()
    let signal: AbortSignal | undefined
    const read = vi.fn(async (path, options) => {
      if (path === BOARD_PATH) return board()
      signal = options.signal
      return new Promise((resolve, reject) => signal!.addEventListener('abort', () => reject(new Error('aborted'))))
    })
    const store = new FactoryStore(resources, read as any)
    store.start(); store.start(); await tick()
    expect(read.mock.calls.filter(([path]) => path === BOARD_PATH)).toHaveLength(1)
    expect(read.mock.calls.filter(([path]) => path.includes('/changes?'))).toHaveLength(1)
    store.stop(); expect(signal!.aborted).toBe(false)
    store.stop(); expect(signal!.aborted).toBe(true)
    await tick(); expect(vi.getTimerCount()).toBe(0)
  })
  it('an action cancels the old feed before refreshing and resumes from the new cursor', async () => {
    vi.useFakeTimers()
    const resources = new Resources()
    const signals: AbortSignal[] = []
    let reads = 0, resolveBoard!: (value: FactoryView) => void
    const read = vi.fn(async (path, options) => {
      if (path === BOARD_PATH) {
        if (!reads++) return board()
        return new Promise<FactoryView>(resolve => { resolveBoard = resolve })
      }
      signals.push(options.signal)
      return new Promise((resolve, reject) => options.signal.addEventListener('abort', () => reject(new Error('aborted'))))
    })
    const store = new FactoryStore(resources, read as any)
    store.start(); await tick()
    resources.invalidate(path => path === BOARD_PATH)
    await tick()
    expect(signals[0]!.aborted).toBe(true)
    expect(signals).toHaveLength(1)
    resolveBoard({ ...board('New decision'), cursor: 'instance:8' }); await tick()
    expect(read.mock.calls.at(-1)![0]).toContain('since=instance%3A8')
    store.stop()
  })
  it('pauses hidden viewers and retries errors without discarding their last known board', async () => {
    vi.useFakeTimers()
    const resources = new Resources()
    const read = vi.fn(async (path) => { if (path === BOARD_PATH) return board(); throw new Error('offline') })
    const store = new FactoryStore(resources, read as any)
    store.start(); await tick()
    expect(store.entry.data.value?.ok).toBe(true); expect(store.entry.error.value).toBe('offline')
    store.visibility(true); await vi.advanceTimersByTimeAsync(20000)
    expect(read).toHaveBeenCalledTimes(2)
    store.visibility(false); await tick(); expect(read).toHaveBeenCalledTimes(3)
    store.stop()
  })
})
