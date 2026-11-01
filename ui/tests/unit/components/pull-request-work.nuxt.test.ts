// @vitest-environment nuxt
import { describe, it, expect } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import PullRequestWorkList from '../../../app/components/PullRequestWorkList.vue'
import IssueWorkerOutput from '../../../app/components/IssueWorkerOutput.vue'
import { currentPullJob, mergePullJobs, type PullRequestWork } from '../../../app/utils/pullRequests'
import { groupIssues, type FactoryView, type JobView } from '../../../app/utils/factory'

const pull: PullRequestWork = { key: 'pr:1:670', repo_id: 1, repo: 'example/anton', number: 670,
  title: 'Correct the session loader', url: 'https://github.com/example/anton/pull/670', unit_id: 'unit',
  candidate: { id: 'candidate', head: 'a'.repeat(40), base: 'b'.repeat(40), base_ref: 'main', pr: 670 },
  status: 'queued', stage_code: 'reviewing', state: 'reviewing', reason: 'Waiting for review capacity',
  reason_code: null, retry_at: null, active_job_id: null, created_at: 1, updated_at: 2, enabled: true }
const job = { id: 'job', candidate_id: 'candidate', created_at: 10, role: 'review', phase: 'cleaned', outcome: 'deferred',
  cleanup: true, issue_key: null, repo_id: 1, repo: 'example/anton', issue_number: null, pull_request: { repo_id: 1, number: 670 } } as JobView

describe('pull request work', () => {
  it('keeps queued and deferred work visible without an active worker or fake issue link', async () => {
    const wrapper = await mountSuspended(PullRequestWorkList, { props: { items: [pull] } })
    expect(wrapper.text()).toContain('Review · Queued')
    await wrapper.setProps({ more: 'cursor:1', recentTruncated: true })
    expect(wrapper.get('a[href="/pull-requests?before=cursor%3A1"]').text()).toContain('View more active reviews')
    expect(wrapper.text()).toContain('1 shown')
    expect(wrapper.text()).toContain('Only recent completed reviews')
    expect(wrapper.get('a[href="/pull-requests/1/670"]').text()).toBe(pull.title)
    expect(wrapper.find('a[href*="/issues/"]').exists()).toBe(false)
    await wrapper.setProps({ items: [{ ...pull, status: 'deferred', reason: 'Dependency downloads are rate limited.', retry_at: 1800000000 }] })
    expect(wrapper.text()).toContain('Waiting to retry')
    expect(wrapper.text()).toContain('Dependency downloads are rate limited.')
    expect(wrapper.get('time').attributes('datetime')).toBe('2027-01-15T08:00:00.000Z')
    await wrapper.setProps({ items: [{ ...pull, status: 'running', stage_code: 'auditing', active_job_id: 'job' }] })
    expect(wrapper.text()).toContain('Audit · Running')
    await wrapper.setProps({ items: [{ ...pull, status: 'completed', stage_code: 'merged', reason: 'Merged on GitHub; remaining Forge work stopped.' }] })
    expect(wrapper.text()).toContain('Merged · Completed')
    expect(wrapper.text()).toContain('remaining Forge work stopped')
    wrapper.unmount()
  })

  it('marks old revision output and keeps deduplicated chronological job history', async () => {
    const old = { ...job, id: 'old', candidate_id: 'prior', created_at: 5 }
    expect(currentPullJob(old, pull)).toBe(false)
    expect(currentPullJob(job, pull)).toBe(true)
    expect(mergePullJobs([job], [old, { ...job, outcome: null }])).toEqual([job, old])
    const wrapper = await mountSuspended(IssueWorkerOutput, { props: {
      jobs: [job, old], selectedJob: old.id, currentCandidateId: 'candidate', open: true,
      output: { ok: true, live: false, text: 'Earlier review output' },
    } })
    expect(wrapper.get('pre').text()).toBe('Earlier review output')
    expect(wrapper.get('a[aria-current="true"]').text()).toContain('earlier revision')
    expect(wrapper.text()).toContain('current revision')
    wrapper.unmount()
  })

  it('does not route issue-less PR workers through internal issue cards', () => {
    const view = { repositories: [], issues: [], jobs: [job] } as unknown as FactoryView
    expect(groupIssues(view, 20).internalJobs).toEqual([])
  })
})
