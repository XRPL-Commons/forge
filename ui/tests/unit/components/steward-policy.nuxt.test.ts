// @vitest-environment nuxt
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mountSuspended, mockNuxtImport } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import SelectMenu from '@nuxt/ui/components/SelectMenu.vue'
import RepositoryStewardPolicy from '../../../app/components/RepositoryStewardPolicy.vue'
const { get, act } = vi.hoisted(() => ({ get: vi.fn(), act: vi.fn() }))
mockNuxtImport('useForgeApi', () => () => ({ get, act }))
let id = 4000

describe('steward repository policy', () => {
  beforeEach(() => { get.mockReset(); act.mockReset() })
  it('reads lazily through the shared cache and saves the exact policy revision', async () => {
    const repoId = ++id
    const value = { ok: true, repo_id: repoId, name: 'owner/repo', policy: 'reversible', revision: 2 }
    get.mockResolvedValue(value)
    act.mockResolvedValue({ ...value, policy: 'manual', revision: 3 })
    const wrapper = await mountSuspended(RepositoryStewardPolicy, { props: { repoId } })
    await flushPromises()
    expect(get).toHaveBeenCalledWith(`/api/repositories/steward?repo_id=${repoId}`)
    wrapper.getComponent(SelectMenu).vm.$emit('update:modelValue', 'manual'); await flushPromises(); await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(act).toHaveBeenCalledWith('/api/repositories/steward', { repo_id: repoId, revision: 2, policy: 'manual' })
    expect(wrapper.getComponent(SelectMenu).props('modelValue')).toBe('manual')
    expect(wrapper.text()).toContain('Saved')
    wrapper.unmount()
  })
  it('does not silently repeat a write after an interrupted reply', async () => {
    const repoId = ++id
    get.mockResolvedValue({ ok: true, repo_id: repoId, name: 'owner/repo', policy: 'manual', revision: 3 })
    act.mockRejectedValue(new Error('lost reply'))
    const wrapper = await mountSuspended(RepositoryStewardPolicy, { props: { repoId } })
    await flushPromises(); wrapper.getComponent(SelectMenu).vm.$emit('update:modelValue', 'reversible'); await flushPromises(); await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(wrapper.text()).toContain('lost reply')
    expect(act).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })
})
