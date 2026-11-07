import { factoryIdentity } from '~/utils/factoryIdentity'

// Auth middleware already fetches this public field. Naming adds no polling.
export function useFactoryIdentity() {
  const { session } = useOperator()
  const identity = computed(() => factoryIdentity(session.value?.factory_name))
  return {
    name: computed(() => identity.value.name),
    named: computed(() => identity.value.named),
    brand: computed(() => identity.value.brand),
  }
}
