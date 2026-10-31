// @vitest-environment node
import { afterEach, expect, it, vi } from 'vitest'
const { navigate, operator } = vi.hoisted(() => ({ navigate: vi.fn(), operator: vi.fn() }))
vi.mock('#app/nuxt', () => ({ useRuntimeConfig: () => ({ public: { forgeSetupPending: true } }) }))
vi.mock('#app/composables/router', () => ({ defineNuxtRouteMiddleware: (handler: unknown) => handler, navigateTo: navigate }))
vi.mock('../../app/composables/useOperator', () => ({ useOperator: operator }))

afterEach(() => vi.unstubAllGlobals())

it('keeps unfinished installations on the setup screen without requesting an unavailable session', async () => {
  const handler = (await import('../../app/middleware/00-operator.global')).default
  await handler({ path: '/settings' } as any, {} as any)
  expect(navigate).toHaveBeenCalledWith('/login', { redirectCode: 303 })
  navigate.mockClear()
  await handler({ path: '/login' } as any, {} as any)
  expect(navigate).not.toHaveBeenCalled()
  expect(operator).not.toHaveBeenCalled()
})
