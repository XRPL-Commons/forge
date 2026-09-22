import { ref, onScopeDispose, getCurrentScope } from 'vue'
export interface PollOptions<T> { interval?: number; onChange?: (value: T) => void; allowed?: () => boolean }
export function usePoll<T>(load: () => Promise<T>, options: PollOptions<T> = {}) {
  const interval = options.interval ?? 5000
  const data = ref<T | null>(null) as import('vue').Ref<T | null>
  const error = ref<string | null>(null); const stale = ref(false)
  let timer: ReturnType<typeof setTimeout> | undefined, pending: Promise<T | null> | null = null, signature = '', running = false
  // Bumped by replace(); a load that started before the bump belongs to a
  // superseded state and is thrown away when it lands.
  let generation = 0
  const hidden = () => typeof document !== 'undefined' && document.hidden
  function schedule(delay = interval) { clearTimeout(timer); if (running && !hidden()) timer = setTimeout(() => { void refresh() }, delay) }
  function refresh(): Promise<T | null> {
    if (pending) return pending
    if (options.allowed && !options.allowed()) { schedule(); return Promise.resolve(data.value) }
    pending = (async () => {
      const started = generation
      try {
        const next = await load(); const key = JSON.stringify(next)
        if (started !== generation) { schedule(); return data.value }
        data.value = next; error.value = null; stale.value = false
        if (key !== signature) { signature = key; options.onChange?.(next) }
        schedule(); return next
      } catch (failure: any) {
        // A failure of a superseded load says nothing about the replaced
        // state either, so it is reported by the next poll or not at all.
        if (started !== generation) { schedule(Math.max(interval, 15000)); return data.value }
        error.value = failure?.message ?? 'Unavailable'; stale.value = data.value !== null; schedule(Math.max(interval, 15000)); return data.value
      } finally { pending = null }
    })()
    return pending
  }
  // Adopts a value the page already has (the response of an action that
  // returns the whole view), so the page does not wait for the next poll and
  // an identical poll afterwards is not announced as a change.
  function replace(value: T) {
    generation += 1
    data.value = value; error.value = null; stale.value = false
    signature = JSON.stringify(value)
  }
  function onVisibility() { clearTimeout(timer); if (!hidden() && running) void refresh() }
  function start() { if (running) return; running = true; document.addEventListener('visibilitychange', onVisibility); if (!hidden()) void refresh() }
  function stop() { running = false; clearTimeout(timer); document.removeEventListener('visibilitychange', onVisibility) }
  if (getCurrentScope()) onScopeDispose(stop)
  return { data, error, stale, refresh, replace, start, stop }
}
