// @vitest-environment nuxt
import { describe, it, expect, vi } from 'vitest'
import { ref, computed, nextTick } from 'vue'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import InstallationPage from '../../../app/pages/installation.vue'
import InstallationBanner from '../../../app/components/InstallationBanner.vue'
import RepositorySetupWizard from '../../../app/components/RepositorySetupWizard.vue'
const state = vi.hoisted(() => ({ current: null as any, repository: null as any }))
vi.mock('../../../app/composables/useRepository', () => ({ useRepository: () => state.repository }))
vi.mock('../../../app/composables/useInstallation', () => ({ useInstallation: () => state.current }))
function setup() {
  const now = Date.now() / 1000
  const view = ref({ available: true, complete: false, generation: 'one', started_at: now - 600, updated_at: now - 90, observed_at: now, source: 'server',
    stages: [{ id: 'environment', label: 'Build environment', status: 'complete' }, { id: 'smoke', label: 'First merge test', status: 'blocked', reason: 'The test needs another build attempt.' }],
    smoke: { status: 'pending', phase: 'review', reason: 'Review is waiting for a worker.', issue_url: 'https://github.com/example/smoke/issues/1', pull_url: 'https://github.com/example/smoke/pull/2' },
    operation: null, actions: [{ id: 'retry', label: 'Retry first merge test' }],
  })
  state.current = { view, pending: computed(() => !view.value.complete), headline: ref('Review is waiting for a worker.'), busy: ref(false), actionError: ref(''), act: vi.fn(),
    poll: { error: ref(null), stale: ref(false), refresh: vi.fn() } }
}
describe('installation progress', () => {
  it('shows real stage, timestamps, issue links and only sends the offered action after a click', async () => {
    setup()
    const wrapper = await mountSuspended(InstallationPage)
    expect(wrapper.text()).toContain('Elapsed 10 min')
    expect(wrapper.text()).toContain('Last setup report')
    expect(wrapper.text()).toContain('Status checked')
    expect(wrapper.text()).toContain('Needs attention')
    expect(wrapper.text()).toContain('Review is waiting for a worker.')
    expect(wrapper.get('a[href="https://github.com/example/smoke/issues/1"]').text()).toBe('View test issue')
    expect(wrapper.get('a[href="https://github.com/example/smoke/pull/2"]').text()).toBe('View pull request')
    expect(state.current.act).not.toHaveBeenCalled()
    await wrapper.findAll('button').find(button => button.text() === 'Retry first merge test')!.trigger('click')
    expect(state.current.act).toHaveBeenCalledWith(state.current.view.value.actions[0])
    wrapper.unmount()
  })
  it('keeps the last checklist visible when refreshing fails without claiming work failed', async () => {
    setup(); state.current.poll.error.value = 'Network unavailable'; state.current.poll.stale.value = true
    const wrapper = await mountSuspended(InstallationPage)
    expect(wrapper.text()).toContain('Showing the last received status. Forge may still be working')
    expect(wrapper.text()).toContain('Build environment')
    expect(wrapper.text()).not.toContain('Your factory is ready')
    wrapper.unmount()
  })
  it('blocks repository activation while showing the route to finish setup', async () => {
    setup()
    state.repository = Object.fromEntries(['view', 'live', 'row', 'report', 'shown', 'displayReady', 'ready', 'prepared', 'pickerRows', 'connections', 'accessDescription', 'accessNote', 'syncing', 'failed', 'hint', 'editUrl', 'downloadHref', 'downloaded', 'busy', 'checking', 'enabling', 'error', 'query'].map(key => [key, ref(null)]))
    Object.assign(state.repository, { live: ref(true), ready: ref(true), row: ref({ id: 1, name: 'example/project', enabled: false, access_ready: true, protection_ready: true, protection_status: 'ready' }), enable: vi.fn() })
    const wrapper = await mountSuspended(RepositorySetupWizard, { props: { initialRepoId: 1 }, global: { stubs: { UModal: { template: '<div><slot name="body" /><slot name="footer" /></div>' } } } })
    expect(wrapper.text()).toContain('Finish the installation test before enabling automation.')
    expect(wrapper.get('a[href="/installation"]').text()).toBe('View setup progress')
    const enable = wrapper.findAll('button').find(button => button.text() === 'Enable automation')!
    expect(enable.attributes('disabled')).toBeDefined()
    await enable.trigger('click')
    expect(state.repository.enable).not.toHaveBeenCalled()
    wrapper.unmount()
  })
  it('keeps a setup entry point visible across the app until installation completes', async () => {
    setup()
    const wrapper = await mountSuspended(InstallationBanner)
    expect(wrapper.get('a[href="/installation"]').text()).toBe('View setup progress')
    state.current.view.value.complete = true
    await nextTick()
    expect(wrapper.find('aside').exists()).toBe(false)
    wrapper.unmount()
  })
})
