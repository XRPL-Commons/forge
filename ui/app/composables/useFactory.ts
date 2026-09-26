import { ForgeApiError } from './useForgeApi'
import type { FactoryView } from '~/utils/factory'
export function useFactory() {
  const api = useForgeApi()
  return useResource<FactoryView | { ok: false; reason: string }>('/api/factory', async (path) => {
    try { return await api.get<FactoryView>(path) }
    catch (error) {
      if (error instanceof ForgeApiError && error.status === 200) return { ok: false, reason: error.message }
      throw error
    }
  })
}
