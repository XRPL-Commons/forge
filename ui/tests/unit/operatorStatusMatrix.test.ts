import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { groupIssues, phaseLabel, reasonText } from '../../app/utils/factory'
import { lineState, lineTooltip } from '../../app/utils/factoryLine'
import { modelCardView, rowCardView } from '../../app/utils/issueCard'
import { statusLabel, type IssueRow } from '../../app/utils/issues'
import { issuePipeline } from '../../app/utils/pipeline'
import type { CanonicalIssueStatus } from '../../app/utils/operatorStatus'

// Python builds each case through typed lifecycle commands and checks Activity,
// list/detail, scoped API and CLI output against this same acceptance fixture.
const fixtureBase = import.meta.url
const matrix = JSON.parse(readFileSync(fileURLToPath(new URL('../../../tests/fixtures/operator-status.json', fixtureBase)), 'utf8')) as { name: string; expected: CanonicalIssueStatus }[]
const now = 100

function views(operator: CanonicalIssueStatus) {
  // Deliberately contradictory legacy fields: a second mapping must never win.
  const issue: any = { key: '1:1', repo_id: 1, repo: 'owner/repo', number: 1, title: 'Issue',
    state: 'approval_needed', active: null, reason: 'Old reason', failure_count: 0,
    last_summary: null, operator_status: operator }
  const view: any = { issues: [issue], jobs: [], repositories: [], observation: { at: now },
    runtime: { slots: {}, cycle_seconds: 30 } }
  const row: IssueRow = { key: '1:1', repo_id: 1, repo: 'owner/repo', number: 1, title: 'Issue',
    content_hash: '', body_truncated: false, state: 'open', updated_at: null, labels: [], assignees: [],
    status: operator.code, operator_status: operator, reason_code: 'obsolete', stage_code: 'reviewing',
    summary: null, explanation: 'Old explanation #99', actions: [], setup_needed: false,
    environment_blocker: null, pull_requests: [], dependencies: [99], fresh: true, revision: 'r',
    setup_url: '', active: null }
  return { issue, view, row }
}

describe('canonical projection/API/UI status matrix', () => {
  for (const { name, expected } of matrix) {
    it(`${name}: Activity, list and detail share the main label and separate reason`, () => {
      const { issue, view, row } = views(expected)
      const groups = groupIssues(view, now)
      const card = groups.attention[0]!
      const activity = modelCardView(card, now)
      const list = rowCardView(row, now)
      // IssueDetail uses exactly this helper for its header badge.
      expect(statusLabel(row)).toBe(expected.label)
      expect(activity.status?.label).toBe(expected.label)
      expect(list.status?.label).toBe(expected.label)
      expect(activity.statusReason).toBe(expected.reason)
      expect(list.statusReason).toBe(expected.reason)
      expect(reasonText(issue, null)).toBe(expected.reason)
      // The factory diagram also used its own attention/queue wording.
      const lineItem = lineState(view, now)!.items[0]!
      expect(lineItem.status).toBe(expected.label)
      expect(lineTooltip(lineItem, false)).toBe(`#1 · ${expected.label} · ${expected.reason}`)
      // A stale job cannot replace a current gate's main label with "Working".
      expect(phaseLabel(issue, { phase: 'running' } as any, view, now)).toBe(expected.label)
      expect(issuePipeline(row).note).toBe(expected.reason)
    })
  }

  it('uses one fallback for pre-cutover list and Activity payloads', () => {
    for (const [state, status, label] of [
      ['ready', 'queued', 'Queued'], ['approval_needed', 'attention', 'Needs attention'],
      ['hold', 'blocked', 'Blocked'], ['investigating', 'waiting', 'Waiting'],
      ['merged', 'completed', 'Completed'], ['cancelled', 'dropped', 'No longer needed'],
    ]) {
      expect(phaseLabel({ state } as any, null, null, now)).toBe(label)
      expect(statusLabel({ status, explanation: '', dependencies: [] } as any)).toBe(label)
    }
  })

  it('uses the canonical current stage while waiting for cleanup or result validation', () => {
    for (const name of ['cleanup_pending', 'result_pending']) {
      const { row } = views(matrix.find(value => value.name === name)!.expected)
      const pipeline = issuePipeline({ ...row, stage_code: 'auditing', ledger_state: 'ready' })
      expect(pipeline.current).toBe('audit')
      expect(pipeline.state).toBe('waiting')
    }
  })

  it('completed elsewhere and reopened issues do not acquire a successful merge stage', () => {
    const completed = issuePipeline(views(matrix.find(value => value.name === 'completed_elsewhere')!.expected).row)
    expect(completed.current).toBeNull()
    expect(completed.steps.every(step => step.state === 'upcoming')).toBe(true)
    const reopened = issuePipeline(views(matrix.find(value => value.name === 'reopened')!.expected).row)
    expect(reopened.current).toBe('queued')
    expect(reopened.steps.some(step => step.state === 'done')).toBe(false)
  })
})
