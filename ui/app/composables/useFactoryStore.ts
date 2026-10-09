import { FactoryStore } from '~/utils/factoryStore'
const stores = new WeakMap<object, FactoryStore>()

export function useFactoryStore() {
  const app = useNuxtApp()
  let store = stores.get(app)
  if (!store) {
    store = new FactoryStore(useResources(), useForgeApi().get)
    stores.set(app, store)
    if (import.meta.client) {
      const current = store
      document.addEventListener('visibilitychange', () => current.visibility(document.hidden))
      current.visibility(document.hidden)
    }
  }
  return store
}

export function useFactoryBoard() {
  const store = useFactoryStore()
  let started = false
  const start = () => { if (!started && import.meta.client) { started = true; store.start() } }
  const stop = () => { if (started) { started = false; store.stop() } }
  onScopeDispose(stop)
  if (import.meta.server) onServerPrefetch(() => store.refresh(5000))
  return { data: computed(() => store.entry.data.value), error: computed(() => store.entry.error.value),
    updatedAt: computed(() => store.entry.updatedAt.value), stale: computed(() => !!store.entry.error.value),
    refresh: (maxAge = 0) => store.refresh(maxAge), start, stop,
    replace: (value: import('~/utils/factoryStore').FactoryPayload) => store.resources.replace('/api/factory?section=board', value) }
}
