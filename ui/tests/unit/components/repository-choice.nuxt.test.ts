// @vitest-environment nuxt
import { describe, expect, it, vi } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import SelectMenu from '@nuxt/ui/components/SelectMenu.vue'
import RepositoryChoiceSetting from '../../../app/components/RepositoryChoiceSetting.vue'

describe('repository choices', () => {
  it('requires Save and reports rejected writes without discarding edits or retrying', async () => {
    const save = vi.fn().mockRejectedValue(new Error('Settings changed. Reload before saving.'))
    const wrapper = await mountSuspended(RepositoryChoiceSetting, { props: {
      id: 'scope', label: 'Review pull requests', value: 'all', save,
      options: [{ value: 'all', label: 'All pull requests' }, { value: 'forge', label: 'Forge only' }],
    } })
    expect(wrapper.find('[type=submit]').exists()).toBe(false)
    wrapper.getComponent(SelectMenu).vm.$emit('update:modelValue', 'forge'); await flushPromises()
    expect(save).not.toHaveBeenCalled()
    await wrapper.setProps({ disabled: true })
    await wrapper.get('form').trigger('submit')
    expect(save).not.toHaveBeenCalled()
    await wrapper.setProps({ disabled: false })
    await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(save).toHaveBeenCalledExactlyOnceWith('forge')
    expect(wrapper.get('[role="alert"]').text()).toContain('Settings changed')
    expect(wrapper.getComponent(SelectMenu).props('modelValue')).toBe('forge')
    expect(wrapper.text()).not.toContain('Saved')
    wrapper.unmount()
  })
  it('preserves edits across observations, supports discard, and prevents duplicate saves', async () => {
    let finish!: () => void
    const save = vi.fn(() => new Promise<void>(resolve => { finish = resolve }))
    const wrapper = await mountSuspended(RepositoryChoiceSetting, { props: {
      id: 'merge-policy', label: 'Merging', value: 'human', save,
      options: [{ value: 'human', label: 'Require approval' }, { value: 'auto', label: 'Automatic' }],
    } })
    const menu = wrapper.getComponent(SelectMenu)
    menu.vm.$emit('update:modelValue', 'auto'); await flushPromises()
    await wrapper.setProps({ value: 'human' })
    expect(menu.props('modelValue')).toBe('auto')
    await wrapper.get('[aria-label="Discard merging changes"]').trigger('click')
    expect(menu.props('modelValue')).toBe('human')
    expect(wrapper.find('[type=submit]').exists()).toBe(false)
    menu.vm.$emit('update:modelValue', 'auto'); await flushPromises()
    await wrapper.get('form').trigger('submit')
    expect(menu.props('disabled')).toBe(true)
    await wrapper.get('form').trigger('submit')
    expect(save).toHaveBeenCalledExactlyOnceWith('auto')
    await wrapper.setProps({ value: 'auto' })
    finish(); await flushPromises()
    expect(wrapper.find('[type=submit]').exists()).toBe(false)
    expect(wrapper.get('[role="status"]').text()).toBe('Saved')
    wrapper.unmount()
  })

})
