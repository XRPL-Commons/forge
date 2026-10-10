import type { HttpTimingSample } from '../utils/httpTiming'

export default defineNuxtPlugin(() => {
  const samples = useState<HttpTimingSample[]>('forge-http-timings', () => [])
  let emitted = 0
  // Includes the initial server fetches hydrated with this page, then actual
  // browser fetches. Each Nuxt app owns its samples and its emission cursor.
  watch(() => samples.value.length, () => {
    while (emitted < samples.value.length) {
      console.debug('[forge-http-timing]', JSON.stringify(samples.value[emitted++]))
    }
  }, { immediate: true })
})
