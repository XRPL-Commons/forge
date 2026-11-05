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
  it.each([
    ['building', 'Building the change'], ['publishing', 'Publishing the pull request'],
    ['reviewing', 'Reviewing the change'], ['auditing', 'Auditing the change'],
    ['merging', 'Merging the pull request'], ['merged', 'Pull request merged'],
  ])('shows the %s phase alongside one operator explanation', async (phase, label) => {
    setup()
    const reason = 'The worker is processing this change.'
    Object.assign(state.current.view.value.smoke, { phase, reason, operator_status: { reason } })
    const wrapper = await mountSuspended(InstallationPage)
    const test = wrapper.get('section[aria-label="First merge test"]')
    expect(test.get('[data-testid="installation-smoke-phase"]').text()).toBe(label)
    expect(test.text().split(reason)).toHaveLength(2)
    wrapper.unmount()
  })
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
    expect(wrapper.text()).toContain('Finish installation before enabling automation.')
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
  it('offers explicit browser consent and sends the offered action only after clicking', async () => {
    setup()
    Object.assign(state.current.view.value, {
      account: { provider: 'codex', status: 'account_login_required', url: 'https://auth.openai.com/codex/device', code: 'TEST-CODE', expires_at: Date.now() / 1000 + 600 },
      actions: [{ id: 'retry_account', label: 'Retry Codex sign-in' }], operation: { status: 'waiting' },
    })
    const wrapper = await mountSuspended(InstallationPage)
    const account = wrapper.get('section[aria-label="Connect Codex"]')
    expect(account.get('code').text()).toBe('TEST-CODE')
    expect(account.get('a[href="https://auth.openai.com/codex/device"]').text()).toBe('Open Codex sign-in')
    expect(wrapper.text()).toContain('Waiting for you. Complete the steps below')
    expect(wrapper.text()).not.toContain('Setup is running.')
    expect(state.current.act).not.toHaveBeenCalled()
    await account.findAll('button').find(button => button.text() === 'Retry Codex sign-in')!.trigger('click')
    expect(state.current.act).toHaveBeenCalledWith(state.current.view.value.actions[0])
    state.current.view.value.account = { status: 'connected', provider: 'codex' }
    state.current.view.value.actions = []
    await nextTick()
    expect(account.text()).toContain('Codex is connected')
    expect(wrapper.text()).not.toContain('TEST-CODE')
    wrapper.unmount()
  })
  it('does not display expired device codes and explains retry', async () => {
    setup()
    state.current.view.value.account = { status: 'account_login_pending', provider: 'codex', url: 'https://auth.openai.com/codex/device', code: 'EXPIRED-CODE', expires_at: Date.now() / 1000 - 5 }
    const wrapper = await mountSuspended(InstallationPage)
    expect(wrapper.text()).not.toContain('EXPIRED-CODE')
    expect(wrapper.text()).toContain('This sign-in attempt expired or failed.')
    wrapper.unmount()
  })
  it('keeps repository configuration available and explains the reported blocker', async () => {
    setup()
    state.current.view.value.preparation = { status: 'needs_operator', stages: [], providers: [], ready: false,
      needs_operator: [{ repository: { id: 1, name: 'owner/project' }, reason: 'Repository access needs refreshing.', path: '/settings/repositories?repo=1' }] }
    const wrapper = await mountSuspended(InstallationPage)
    expect(wrapper.get('a[href="/settings/repositories"]').text()).toBe('Configure repositories')
    const section = wrapper.get('section[aria-label="Repository setup needs attention"]')
    expect(section.text()).toContain('Repository access needs refreshing.')
    expect(section.get('a[href="/settings/repositories?repo=1"]').text()).toBe('Review repository setup')
    expect(section.text()).toContain('Continue setup.')
    wrapper.unmount()
  })

})
