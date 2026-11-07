interface OperatorSession {
  ok: boolean
  enabled: boolean
  factory_name?: string | null
  operator: { id: number; login: string } | null
  controls_token?: string
}

// Per-app cache (isolated for SSR); authorization itself is checked on every API request.
export function useOperator() {
  const session = useState<OperatorSession | null>('operator-session', () => null)
  const checked = useState<number>('operator-checked', () => 0)
  const request = useRequestFetch()
  const api = useForgeApi()
  async function refresh() {
    const value = await request<OperatorSession>('/api/auth/session', { timeout: 5000 })
    if (!value?.ok || typeof value.enabled !== 'boolean') throw new Error('Sign-in service is unavailable')
    session.value = value
    checked.value = Date.now()
    if (value.controls_token) api.token.value = value.controls_token
    return value
  }
  async function logout() {
    await api.act('/api/auth/logout', {})
    // A full navigation discards every cached private response and SSR payload.
    window.location.replace('/login')
  }
  return { session, checked, refresh, logout }
}
