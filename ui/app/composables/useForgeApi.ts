export class ForgeApiError extends Error { constructor(public status: number, message: string) { super(message) } }
const token = () => useState<string>('forge-action-token', () => '')

function checkOk(status: number, payload: any, fallback: string) {
  if (payload && payload.ok === false) throw new ForgeApiError(status, payload.reason ?? fallback)
}

export interface GetOptions { timeout?: number; signal?: AbortSignal }

export function useForgeApi() {
  const current = token()
  const resources = useResources()
  const redirecting = useState<boolean>('forge-sign-in-redirecting', () => false)
  const forwarded = useRequestHeaders(['cookie', 'host'])
  function signIn() {
    // Several poll requests can expire together. Replacing the navigation on
    // every 401 can prevent the sign-in page from finishing its load.
    if (import.meta.client && !redirecting.value) {
      redirecting.value = true
      window.location.replace('/login')
    }
  }
  async function get<T>(path: string, options: GetOptions = {}): Promise<T> {
    const entry = resources.entry(path)
    const generation = entry.generation
    const cached = entry.response
    let response
    try {
      response = await $fetch.raw<T & { ok?: boolean; reason?: string; controls_token?: string }>(path, {
        method: 'GET', credentials: 'same-origin',
        headers: { ...forwarded, ...(cached ? { 'If-None-Match': cached.etag } : {}) }, signal: options.signal ? AbortSignal.any([options.signal, AbortSignal.timeout(options.timeout ?? 15000)]) : AbortSignal.timeout(options.timeout ?? 15000) })
    } catch (error: any) {
      const status = error?.response?.status ?? 0, reason = error?.response?._data?.reason
      if (status === 401) {
        resources.invalidate(() => true, true)
        signIn()
      }
      throw new ForgeApiError(status, reason ?? 'Request failed')
    }
    if (response.status === 304 && !cached) throw new ForgeApiError(304, 'Refresh to reload this view.')
    const payload = (response.status === 304 ? cached!.data : response._data) as any
    const etag = response.headers.get('etag')
    if (entry.generation === generation && response.status !== 304) entry.response = etag && payload?.ok !== false ? { etag, data: payload } : undefined
    checkOk(response.status, payload, 'Request failed')
    if (entry.generation === generation && payload?.controls_token) current.value = payload.controls_token
    return payload as T
  }
  async function act<T>(path: string, body: object): Promise<T> {
    const clear = path === "/api/onboarding" && (body as { action?: string }).action === "reset"
    resources.invalidate(() => true, clear)
    let response
    try {
      response = await $fetch.raw<T & { ok?: boolean; reason?: string }>(path, { method: 'POST', body, credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-Forge-Action': current.value } })
    } catch (error: any) {
      const status = error?.response?.status ?? 0, reason = error?.response?._data?.reason
      resources.invalidate(() => true, status === 401 || status === 403 || clear)
      if (status === 401) signIn()
      throw new ForgeApiError(status, reason ?? (status === 403 ? 'This page is out of date. Refresh and try again.' : 'Forge did not accept the action.'))
    }
    const payload = response._data as any
    resources.invalidate(() => true, clear)
    checkOk(response.status, payload, 'Forge did not accept the action.')
    return payload as T
  }
  return { get, act, token: current }
}
