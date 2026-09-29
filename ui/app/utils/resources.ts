import { shallowRef, type ShallowRef } from 'vue'

export interface Resource<T = any> {
  data: ShallowRef<T | null>; error: ShallowRef<string | null>; updatedAt: ShallowRef<number>
  response?: { etag: string; data: unknown }
  pending: Promise<T | null> | null; generation: number; touched: number
  listeners: Set<() => void>; timer?: ReturnType<typeof setTimeout>
}
// Constructed per Nuxt app/request, never as a process-wide data cache.
export class Resources {
  entries = new Map<string, Resource>()
  constructor(private clock = Date.now, private limit = 80, private retention = 300000) {}
  entry<T>(key: string): Resource<T> {
    this.prune()
    let value = this.entries.get(key)
    if (!value) {
      value = { data: shallowRef(null), error: shallowRef(null), updatedAt: shallowRef(0), pending: null,
        generation: 0, touched: this.clock(), listeners: new Set() }
      this.entries.set(key, value)
    }
    value.touched = this.clock()
    return value
  }
  prune() {
    const idle = [...this.entries].filter(([, e]) => !e.listeners.size && !e.pending).sort((a, b) => a[1].touched - b[1].touched)
    for (const [key, e] of idle) {
      if (this.entries.size < this.limit && this.clock() - e.touched < this.retention) break
      clearTimeout(e.timer)
      this.entries.delete(key)
    }
  }
  async load<T>(key: string, loader: () => Promise<T>, maxAge = 0): Promise<T | null> {
    const entry = this.entry<T>(key)
    if (entry.data.value !== null && entry.updatedAt.value && this.clock() - entry.updatedAt.value < maxAge) return entry.data.value
    if (entry.pending) return entry.pending
    const generation = entry.generation
    entry.pending = (async () => {
      try {
        const data = await Promise.resolve().then(loader)
        if (entry.generation === generation) {
          // Preserve object identity and DOM/computed stability on unchanged polls.
          if (JSON.stringify(entry.data.value) !== JSON.stringify(data)) entry.data.value = data
          entry.error.value = null; entry.updatedAt.value = this.clock()
        }
      } catch (error) {
        if (entry.generation === generation) entry.error.value = error instanceof Error ? error.message : 'Unavailable'
      } finally {
        entry.pending = null
        // An action can invalidate while this request is still in flight.
        if (entry.generation !== generation) for (const wake of entry.listeners) wake()
      }
      return entry.data.value
    })()
    return entry.pending
  }
  replace<T>(key: string, value: T) {
    const entry = this.entry<T>(key)
    entry.generation++; entry.data.value = value; entry.error.value = null; entry.updatedAt.value = this.clock()
  }
  invalidate(matches: (key: string) => boolean = () => true, clear = false) {
    for (const [key, entry] of this.entries) if (matches(key)) {
      entry.generation++; entry.updatedAt.value = 0; entry.response = undefined
      if (clear) { entry.data.value = null; entry.error.value = null }
      for (const wake of entry.listeners) wake()
    }
  }
}
