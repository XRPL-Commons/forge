// @vitest-environment nuxt
import { describe, it, expect } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import RepositoryRuleFixCard from '../../../app/components/RepositoryRuleFixCard.vue'
import RepositoryRulesPanel from '../../../app/components/RepositoryRulesPanel.vue'
import type { ProtectionReport, RepositoryRow } from '../../../app/utils/repositories'

const source = { kind: 'ruleset' as const, name: 'Release reviews', id: 10, source_type: 'Repository', bypass_actors_visible: true, url: 'https://github.com/example/project/rules/10' }
const props = { source, repo: 'example/project', branch: 'main', appId: 123, appSlug: 'forge-controller', prefix: '', fixed: false }

describe('review rule fixes', () => {
  it('starts with one command, switches to manual steps and reports a completed check', async () => {
    const wrapper = await mountSuspended(RepositoryRuleFixCard, { props })
    expect(wrapper.get('#bypass-command').text()).toContain('gh api --method PUT repos/example/project/rulesets/10')
    const tab = wrapper.findAll('[role="tab"]').find(item => item.text() === 'Manual steps')!
    await tab.trigger('keydown', { key: 'Enter' })
    expect(wrapper.text()).toContain('For pull requests only')
    expect(wrapper.get('#bypass-edit').attributes('href')).toBe(source.url)
    await wrapper.setProps({ fixed: true })
    expect(wrapper.text()).toContain('Fixed')
    expect(wrapper.find('[role="tablist"]').exists()).toBe(false)
    wrapper.unmount()
  })
  it('provides only manual steps when the complete bypass list is hidden', async () => {
    const wrapper = await mountSuspended(RepositoryRuleFixCard, { props: { ...props, source: { ...source, bypass_actors_visible: false } } })
    expect(wrapper.find('#bypass-command').exists()).toBe(false)
    expect(wrapper.text()).toContain('Add bypass')
    wrapper.unmount()
  })
  it('keeps the same fix card after a successful check and shows it as fixed', async () => {
    const report = { branch: 'main', requirements: [], sources: [source], bypass: { status: 'missing', controller_app_id: 123, sources: [source] } } as unknown as ProtectionReport
    const row = { id: 1, name: 'example/project', protection_status: 'needs_setup', protection_ready: false } as RepositoryRow
    const wrapper = await mountSuspended(RepositoryRulesPanel, { props: { row, live: true, report, displayReady: false, shown: 'needs_setup', failed: [], hint: '', prepared: true, editUrl: '', downloaded: false, controllerSlug: 'forge-controller' } })
    expect(wrapper.findAll('[data-rule-fix]')).toHaveLength(1)
    await wrapper.setProps({ report: { ...report, bypass: { ...report.bypass, status: 'ready', sources: [] } }, displayReady: true })
    expect(wrapper.findAll('[data-rule-fix]')).toHaveLength(1)
    expect(wrapper.text()).toContain('Fixed')
    expect(wrapper.find('#bypass-command').exists()).toBe(false)
    wrapper.unmount()
  })
})
