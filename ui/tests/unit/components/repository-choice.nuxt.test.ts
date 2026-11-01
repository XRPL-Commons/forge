// @vitest-environment nuxt
import { describe, expect, it, vi } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import RepositoryChoiceSetting from '../../../app/components/RepositoryChoiceSetting.vue'

describe('repository choices', () => {
  it('requires Save and reports rejected writes without discarding edits or retrying', async () => {
    const save = vi.fn().mockRejectedValue(new Error('Settings changed. Reload before saving.'))
    const wrapper = await mountSuspended(RepositoryChoiceSetting, { props: {
      id: 'scope', label: 'Review pull requests', value: 'all', save,
      options: [{ value: 'all', label: 'All pull requests' }, { value: 'forge', label: 'Forge only' }],
    } })
    await wrapper.get('select').setValue('forge')
    expect(save).not.toHaveBeenCalled()
    await wrapper.setProps({ disabled: true })
    await wrapper.get('form').trigger('submit')
    expect(save).not.toHaveBeenCalled()
    await wrapper.setProps({ disabled: false })
    await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(save).toHaveBeenCalledExactlyOnceWith('forge')
    expect(wrapper.get('[role="alert"]').text()).toContain('Settings changed')
    expect((wrapper.get('select').element as HTMLSelectElement).value).toBe('forge')
    expect(wrapper.text()).not.toContain('Saved')
    wrapper.unmount()
  })
})
