export class ForgeApiError extends Error { constructor(public status: number, message: string) { super(message) } }
const token = () => useState<string>('forge-action-token', () => '')

export function useForgeApi() {
  const current = token()
  async function get<T>(path: string): Promise<T> {
    const response = await $fetch.raw<T & { ok?: boolean; reason?: string; controls_token?: string }>(path, { method: 'GET', credentials: 'same-origin' })
    const payload = response._data as any
    if (!response.ok || (payload && payload.ok === false)) throw new ForgeApiError(response.status, payload?.reason ?? 'Request failed')
    if (payload?.controls_token) current.value = payload.controls_token
    return payload as T
  }
  async function act<T>(path: string, body: object): Promise<T> {
    let response
    try {
      response = await $fetch.raw<T & { ok?: boolean; reason?: string }>(path, { method: 'POST', body, credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-Forge-Action': current.value } })
    } catch (error: any) {
      const status = error?.response?.status ?? 0, reason = error?.response?._data?.reason
      throw new ForgeApiError(status, reason ?? (status === 403 ? 'This page is out of date. Refresh and try again.' : 'Forge did not accept the action.'))
    }
    const payload = response._data as any
    if (payload && payload.ok === false) throw new ForgeApiError(response.status, payload.reason ?? 'Forge did not accept the action.')
    return payload as T
  }
  return { get, act, token: current }
}
