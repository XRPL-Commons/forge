// @vitest-environment node
import { beforeEach, expect, it, vi } from 'vitest'
const { navigate, operator, config } = vi.hoisted(() => ({ navigate: vi.fn(), operator: vi.fn(), config: { public: { forgeSetupPending: true } } }))
vi.mock('#app/nuxt', () => ({ useRuntimeConfig: () => config }))
vi.mock('#app/composables/router', () => ({ defineNuxtRouteMiddleware: (handler: unknown) => handler, navigateTo: navigate }))
vi.mock('../../app/composables/useOperator', () => ({ useOperator: operator }))

beforeEach(() => {
  navigate.mockClear()
  config.public.forgeSetupPending = true
  operator.mockReturnValue({ session: { value: null }, checked: { value: 0 }, refresh: vi.fn() })
})
const handler = async (path: string, query: Record<string, unknown> = {}) =>
  (await import('../../app/middleware/00-operator.global')).default({ path, fullPath: path, query } as any, {} as any)

it('keeps unfinished installations on login and preserves the installation destination', async () => {
  await handler('/installation')
  expect(navigate).toHaveBeenCalledWith('/login?return_to=%2Finstallation', { redirectCode: 303 })
  navigate.mockClear()
  await handler('/login')
  expect(navigate).not.toHaveBeenCalled()
  expect(operator().refresh).not.toHaveBeenCalled()
})

it('lets a live authenticated session supersede the old startup hint', async () => {
  operator.mockReturnValue({ session: { value: { enabled: true, operator: { id: 1 } } }, checked: { value: Date.now() } })
  await handler('/installation')
  expect(navigate).not.toHaveBeenCalled()
  await handler('/login')
  expect(navigate).toHaveBeenCalledWith('/installation', { redirectCode: 303 })
  await handler('/login', { return_to: '/settings/agents?code=ABCD-EFGH' })
  expect(navigate).toHaveBeenLastCalledWith('/settings/agents?code=ABCD-EFGH', { redirectCode: 303 })
})

it('preserves installation return paths after setup and refuses external destinations', async () => {
  config.public.forgeSetupPending = false
  operator.mockReturnValue({ session: { value: { enabled: true, operator: null } }, checked: { value: Date.now() } })
  await handler('/installation')
  expect(navigate).toHaveBeenCalledWith('/login?return_to=%2Finstallation', { redirectCode: 303 })
  operator.mockReturnValue({ session: { value: { enabled: true, operator: { id: 1 } } }, checked: { value: Date.now() } })
  await handler('/login', { return_to: '//external.example' })
  expect(navigate).toHaveBeenLastCalledWith('/', { redirectCode: 303 })
})
