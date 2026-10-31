// @vitest-environment nuxt
import { describe, it, expect } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import RepositoryProtectionPolicy from '../../../app/components/RepositoryProtectionPolicy.vue'
import RepositoryRulesPanel from '../../../app/components/RepositoryRulesPanel.vue'
import type { RepositoryRow, ProtectionReport } from '../../../app/utils/repositories'

describe('optional GitHub protection', () => {
  it('explains the enforcement choice and emits an explicit change', async () => {
    const wrapper = await mountSuspended(RepositoryProtectionPolicy, { props: { mode: 'required', busy: false } })
    await wrapper.get('select').setValue('optional')
    expect(wrapper.emitted('change')).toEqual([['optional']])
    await wrapper.setProps({ mode: 'optional', busy: true })
    expect(wrapper.text()).toContain('Other writers may merge without Forge’s checks')
    expect(wrapper.text()).toContain('Existing GitHub rules still apply')
    expect(wrapper.get('select').attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })

  it('does not suggest adding bypasses or importing a baseline in optional mode', async () => {
    const source = { kind: 'ruleset' as const, name: 'Existing reviews', id: 10, url: 'https://github.com/example/project/rules/10' }
    const report = { branch: 'main', requirements: [], sources: [source], bypass: { status: 'missing', controller_app_id: 123, sources: [source] } } as unknown as ProtectionReport
    const row = { id: 1, name: 'example/project', github_protection: 'required', protection_status: 'needs_setup' } as RepositoryRow
    const wrapper = await mountSuspended(RepositoryRulesPanel, { props: { row, live: true, report, displayReady: false, shown: 'needs_setup', failed: [], hint: '', prepared: true, editUrl: '', downloaded: false, instructions: true } })
    expect(wrapper.find('[data-rule-fix]').exists()).toBe(true)
    await wrapper.setProps({ row: { ...row, github_protection: 'optional' } })
    expect(wrapper.find('[data-rule-fix]').exists()).toBe(false)
    expect(wrapper.find('#steps').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('Create the review rules')
    wrapper.unmount()
  })
})
