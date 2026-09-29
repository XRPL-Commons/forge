/** Render a stable UTC timestamp until hydration finishes, then use the operator's locale. */
export function useLocalTime() {
  const mounted = ref(false)
  onMounted(() => { mounted.value = true })
  return (at: number, timeOnly = false): string => {
    const date = new Date(at * 1000)
    if (!mounted.value) return date.toISOString()
    return timeOnly ? date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : date.toLocaleString()
  }
}
