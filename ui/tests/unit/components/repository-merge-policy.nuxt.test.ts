// @vitest-environment nuxt
import { describe, it, expect } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import RepositoryRulesPanel from '../../../app/components/RepositoryRulesPanel.vue'
import type { RepositoryRow, ProtectionReport } from '../../../app/utils/repositories'

const source = { kind: 'ruleset' as const, name: 'Owner release rules', id: 10, url: 'https://github.com/example/project/rules/10' }
const row = { id: 1, name: 'example/project', protection_status: 'ready', merge_policy: 'auto', merge_check_scope: 'all', rules_url: 'https://github.com/example/project/settings/rules' } as RepositoryRow
const report = { branch: 'main', available: true, required_approvals: 2, code_owner_reviews: true,
  requirements: [], sources: [source], additional_rules: [{ type: 'required_signatures' }], errors: [],
  bypass: { status: 'missing', controller_app_id: 123, sources: [source] } } as unknown as ProtectionReport
const props = { row, live: true, report, displayReady: true, hint: '' }

describe('supervisor merge policy', () => {
  it('shows owner protections as observations without a protection toggle or bypass instructions', async () => {
    const wrapper = await mountSuspended(RepositoryRulesPanel, { props })
    expect(wrapper.text()).toContain('The supervisor merges Forge work after CI, independent Review and Audit pass.')
    expect(wrapper.text()).toContain('2 approving reviews are required by GitHub.')
    expect(wrapper.text()).toContain('Code owner approval is required.')
    expect(wrapper.text()).toContain('required signatures')
    expect(wrapper.get(`a[href="${source.url}"]`).text()).toContain(source.name)
    expect(wrapper.find('select').exists()).toBe(false)
    expect(wrapper.find('[download], [data-rule-fix]').exists()).toBe(false)
    expect(wrapper.text()).not.toMatch(/bypass|Import a ruleset|Download ruleset/)
    wrapper.unmount()
  })

  it('keeps human approval and review scope separate from owner GitHub rules', async () => {
    const wrapper = await mountSuspended(RepositoryRulesPanel, { props: { ...props, row: { ...row, merge_policy: 'human', merge_check_scope: 'forge' } } })
    expect(wrapper.text()).toContain('A counted human approval is also required before merge.')
    expect(wrapper.text()).toContain('Forge Review and Audit apply to Forge-created pull requests.')
    expect(wrapper.text()).toContain('Repository owners manage these rules.')
    wrapper.unmount()
  })

  it('does not mistake unavailable evidence for absent GitHub protections', async () => {
    const wrapper = await mountSuspended(RepositoryRulesPanel, { props: { ...props, displayReady: false, report: null, hint: 'GitHub could not be reached.' } })
    expect(wrapper.text()).toContain('Current GitHub rules have not been observed yet.')
    expect(wrapper.text()).toContain('GitHub could not be reached.')
    expect(wrapper.text()).not.toContain('No rules')
    wrapper.unmount()
  })
})
