import { ref, onScopeDispose, getCurrentScope } from 'vue'
export interface PollOptions<T> { interval?: number; onChange?: (value: T) => void; allowed?: () => boolean }
export function usePoll<T>(load: () => Promise<T>, options: PollOptions<T> = {}) {
  const interval = options.interval ?? 5000
  const data = ref<T | null>(null) as import('vue').Ref<T | null>
  const error = ref<string | null>(null); const stale = ref(false)
  let timer: ReturnType<typeof setTimeout> | undefined, pending: Promise<T | null> | null = null, signature = '', running = false
  const hidden = () => typeof document !== 'undefined' && document.hidden
  function schedule(delay = interval) { clearTimeout(timer); if (running && !hidden()) timer = setTimeout(() => { void refresh() }, delay) }
  function refresh(): Promise<T | null> {
    if (pending) return pending
    if (options.allowed && !options.allowed()) { schedule(); return Promise.resolve(data.value) }
    pending = (async () => {
      try {
        const next = await load(); const key = JSON.stringify(next)
        data.value = next; error.value = null; stale.value = false
        if (key !== signature) { signature = key; options.onChange?.(next) }
        schedule(); return next
      } catch (failure: any) {
        error.value = failure?.message ?? 'Unavailable'; stale.value = data.value !== null; schedule(Math.max(interval, 15000)); return data.value
      } finally { pending = null }
    })()
    return pending
  }
  function onVisibility() { clearTimeout(timer); if (!hidden() && running) void refresh() }
  function start() { if (running) return; running = true; document.addEventListener('visibilitychange', onVisibility); if (!hidden()) void refresh() }
  function stop() { running = false; clearTimeout(timer); document.removeEventListener('visibilitychange', onVisibility) }
  if (getCurrentScope()) onScopeDispose(stop)
  return { data, error, stale, refresh, start, stop }
}
