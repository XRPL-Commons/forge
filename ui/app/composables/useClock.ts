/** Match the server's first render, then update relative ages while visible. */
export function useClock(interval = 1000) {
  const initial = useState('forge-rendered-at', () => Math.floor(Date.now() / 1000))
  const now = ref(initial.value)
  let timer: ReturnType<typeof setInterval> | undefined
  onMounted(() => {
    now.value = Math.floor(Date.now() / 1000)
    timer = setInterval(() => { if (!document.hidden) now.value = Math.floor(Date.now() / 1000) }, interval)
  })
  onScopeDispose(() => clearInterval(timer))
  return now
}
