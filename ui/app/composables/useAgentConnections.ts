export interface AgentScope {
  repositories: { id: number; name: string }[]
  owners: { id: number; login: string }[]
}
export interface AgentConnection {
  id: string; name: string; operator: { id: number; login: string }
  capabilities: string[]; scope: AgentScope; created: number; expires: number
  idle_expires: number; last_used: number | null; status: string
}
export interface ConnectionRequest {
  name: string; user_code: string; capabilities: string[]; scope: AgentScope; expires: number; origin: string
}
interface ConnectionsView { enabled: boolean; connections: AgentConnection[]; reason?: string }
export const agentCapabilityLabels: Record<string, string> = {
  'repositories:read': 'Read repository setup and readiness',
  'repositories:prepare': 'Request repository verification',
  'repositories:activate': 'Enable or disable repository automation',
}
export function useAgentConnections() {
  const api = useForgeApi()
  const resource = useResource<ConnectionsView>('/api/connections', path => api.get(path), { interval: 15000, maxAge: 15000 })
  onMounted(resource.start)
  return { ...resource,
    preview: (code: string) => api.act<ConnectionRequest>('/api/connections/preview', { code }),
    decide: (request: ConnectionRequest, decision: 'approve' | 'deny', capabilities: string[], scope: AgentScope) =>
      api.act('/api/connections/decide', { code: request.user_code, decision, capabilities, scope, confirmed: true }),
    revoke: (id: string) => api.act('/api/connections/revoke', { id }),
  }
}
