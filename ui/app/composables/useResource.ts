import { computed, onScopeDispose, toValue, watch, type MaybeRefOrGetter } from 'vue'
import { Resources } from '~/utils/resources'

const stores = new WeakMap<object, Resources>()
export function useResources() {
  const app = useNuxtApp()
  let store = stores.get(app)
  if (!store) { store = new Resources(); stores.set(app, store) }
  return store
}

/** One shared value and in-flight request per URL; one active poll owner per resource. */
export function useResource<T>(key: MaybeRefOrGetter<string>, loader: (key: string) => Promise<T>,
  options: { interval?: number; allowed?: () => boolean; maxAge?: number } = {}) {
  const store = useResources()
  const selected = computed(() => store.entry<T>(toValue(key)))
  const interval = options.interval ?? useRuntimeConfig().public.pollInterval
  let started = false
  const hidden = () => typeof document !== 'undefined' && document.hidden
  function schedule() {
    const entry = selected.value
    if (entry.listeners.values().next().value !== wake) return
    clearTimeout(entry.timer)
    if (started && !hidden()) entry.timer = setTimeout(() => { void refresh() }, entry.error.value ? Math.max(interval, 15000) : interval)
  }
  async function refresh(maxAge = 0): Promise<T | null> {
    if (options.allowed && !options.allowed()) { schedule(); return selected.value.data.value }
    const requested = toValue(key)
    const result = await store.load(requested, () => loader(requested), maxAge)
    schedule(); return result
  }
  function wake() { if (started && !hidden()) void refresh(100) }
  function visibility() { if (selected.value.listeners.values().next().value === wake) clearTimeout(selected.value.timer); if (!hidden()) wake() }
  function attach() {
    selected.value.listeners.add(wake)
    if (!hidden()) void refresh(options.maxAge ?? interval)
  }
  function start() {
    if (started || typeof document === 'undefined') return
    started = true; document.addEventListener('visibilitychange', visibility); attach()
  }
  function stop() {
    started = false; detach(selected.value)
    if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', visibility)
  }
  function detach(entry: ReturnType<typeof store.entry>) {
    const owner = entry.listeners.values().next().value === wake
    entry.listeners.delete(wake)
    if (owner) { clearTimeout(entry.timer); entry.listeners.values().next().value?.() }
  }
  watch(() => toValue(key), (_, old) => {
    const entry = store.entries.get(old)
    if (entry) detach(entry)
    if (started) attach()
  })
  onScopeDispose(stop)
  return { data: computed(() => selected.value.data.value), error: computed(() => selected.value.error.value),
    stale: computed(() => !!selected.value.data.value && (!!selected.value.error.value || !selected.value.updatedAt.value)),
    updatedAt: computed(() => selected.value.updatedAt.value), refresh, start, stop,
    replace: (value: T) => store.replace(toValue(key), value) }
}
