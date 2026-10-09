import { describe, it, expect } from 'vitest'
import { LINE_LOOKS, lineState, type LineItem } from '../../app/utils/factoryLine'
import { lineBayCard, lineIssueCard, lineListCard, lineStatus, placeCard } from '../../app/utils/lineCard'
import { needsAction, needsCounts, needsFiltered, needsYouRows, unblocksLabel } from '../../app/utils/needsYou'

const now = 1_000_000

function issue(number: number, over: Record<string, unknown> = {}): any {
  return {
    key: `1:${number}`, repo_id: 1, repo: 'XRPL-Commons/thegame', number, title: `Issue ${number}`,
    state: 'ready', reason: null, active: null, failure_count: 0, history: [], attention_kind: null, ...over,
  }
}

function job(number: number, role: string, over: Record<string, unknown> = {}): any {
  return {
    id: `job-${number}`, issue_key: `1:${number}`, repo_id: 1, repo: 'XRPL-Commons/thegame', issue_number: number,
    role, slot: `${role}-1`, provider: 'claude', provider_id: null, model: null, phase: 'running', outcome: null,
    cleanup: false, fenced: false, created_at: now - 300, deadline: now + 1600, observed_status: 'running', observed_at: now - 10, started_at: now - 240, ...over,
  }
}

function view(issues: any[], jobs: any[] = []): any {
  return {
    ok: true, controls_token: 'synthetic', active: true, operator: null,
    repositories: [{ id: 1, name: 'XRPL-Commons/thegame', enabled: true }],
    observation: { at: now - 10, errors: [] }, actions: [], issues, jobs,
    runtime: { enabled: true, slots: { build: 2, review: 1, audit: 1, recovery: 1 }, cycle_seconds: 30 },
    runtime_revision: 'rev',
  }
}

// #90 ran out of attempts and five issues wait on it (two directly, the
// rest through them); the worker on #104 asked a question and two wait on
// it; #7 waits for an approval, #5 for a setup fix; #9 is deliberately paused.
function factory(): any {
  return view([
    issue(7, { state: 'approval_needed', reason: 'A human review is required before merging.', attention_kind: 'approve' }),
    issue(104, { state: 'hold', reason: 'Which certificate format should it use?', title: 'Rework the certificate flow', attention_kind: 'question' }),
    issue(90, { state: 'hold', failure_count: 3, reason: 'Repeated failure requires a supported change of approach', title: 'Fix the scoring engine', attention_kind: 'approach' }),
    issue(5, { state: 'hold', reason: 'Forge environment needs attention (toolchain)', attention_kind: 'env' }),
    issue(9, { state: 'hold', reason: 'Paused by an operator' }),
    issue(1, { blocked_dependencies: [90] }), issue(2, { blocked_dependencies: [90] }),
    issue(3, { blocked_dependencies: [1] }), issue(4, { blocked_dependencies: [2, 3] }), issue(6, { blocked_dependencies: [4, 104] }),
    issue(8, { blocked_dependencies: [104] }),
    issue(20, { active: 'job-20' }),
  ], [job(20, 'build')])
}

const itemOf = (state: any, key: string): LineItem => state.items.find((item: LineItem) => item.id === key)

describe('needsYouRows', () => {
  it('ranks the crate by what each issue unblocks, then by bay', () => {
    const v = factory()
    const rows = needsYouRows(lineState(v, now), v)
    expect(rows.map(row => [row.item.number, row.reason, row.unblocks])).toEqual([
      [90, 'approach', 5], [104, 'question', 2], [7, 'approve', 0], [5, 'env', 0],
    ])
    expect(needsCounts(rows)).toEqual({ all: 4, approach: 1, question: 1, approve: 1, verify: 0, failed: 0, env: 1, review: 0 })
    expect(needsFiltered(rows, 'question').map(row => row.item.number)).toEqual([104])
    expect(needsFiltered(rows, 'all')).toHaveLength(4)
    expect(needsYouRows(null, v)).toEqual([])
  })

  it('names one action per bay and labels the count', () => {
    expect(needsAction('approve')).toEqual({ label: 'Approve…', decision: true })
    expect(needsAction('question').label).toBe('Answer…')
    expect(needsAction('approach').label).toBe('Describe approach…')
    expect(needsAction('failed').label).toBe('Retry…')
    expect(needsAction('env').label).toBe('Fix setup')
    expect(needsAction('review').label).toBe('Review')
    expect(needsAction('verify').label).toBe('Verify…')
    expect(unblocksLabel(12)).toBe('Unblocks 12')
    expect(unblocksLabel(0)).toBe('')
  })
})

describe('the hover card', () => {
  const v = factory()
  const state = lineState(v, now)!
  const raw = (n: number) => v.issues.find((i: any) => i.number === n)

  it('says what an issue needs, its stage and what it unblocks', () => {
    const card = lineIssueCard(itemOf(state, '1:90'), raw(90), 5)
    expect(card).toMatchObject({
      kind: 'issue', reason: 'approach', ref: 'XRPL-Commons/thegame · #90', title: 'Fix the scoring engine',
      status: 'Out of attempts: describe a different approach, then retry',
      detail: 'Failed 3 of 3 · Repeated failure requires a supported change of approach',
      stage: 'Needs attention', swatch: LINE_LOOKS.attention.fill, unblocks: 5, hint: 'Click to open',
    })
    expect(lineIssueCard(itemOf(state, '1:90'), raw(90), 5, false).ref).toBe('#90')
  })

  it('reads every kind of status in plain words', () => {
    expect(lineStatus(itemOf(state, '1:104'), raw(104))).toBe('The worker asked you something')
    expect(lineStatus(itemOf(state, '1:7'), raw(7))).toBe('A pull request waits for your approval')
    expect(lineStatus(itemOf(state, '1:5'), raw(5))).toBe("Forge's setup needs repair")
    expect(lineStatus(itemOf(state, '1:9'), raw(9))).toBe('On hold')
    expect(lineStatus(itemOf(state, '1:4'), raw(4))).toBe('Waiting on #2, #3')
    expect(lineStatus(itemOf(state, '1:20'), raw(20))).toBe('Building · 4 min')
  })

  it('drops a detail that repeats the status and never shows a negative count', () => {
    const card = lineIssueCard(itemOf(state, '1:4'), raw(4), -1)
    expect(card.detail).toBe('')
    expect(card.unblocks).toBe(0)
    expect(card.reason).toBeNull()
    expect(card.stage).toBe('Waiting on other issues')
  })

  it('lists a bay: number and title, six at most, then the rest as a count', () => {
    const bay = lineBayCard('question', state.items)
    expect(bay).toMatchObject({ kind: 'list', reason: 'question', heading: '1 · question', sub: 'The worker asked you something', more: 0 })
    expect(bay.rows).toEqual([{ id: '1:104', ref: '#104', title: 'Rework the certificate flow' }])
    const many = lineListCard('9 need you', Array.from({ length: 9 }, () => itemOf(state, '1:90')))
    expect(many.rows).toHaveLength(6)
    expect(many.more).toBe(3)
  })
})

describe('placeCard', () => {
  const viewport = { w: 1280, h: 800 }
  const size = { w: 280, h: 120 }

  it('sits centred above the anchor when it fits', () => {
    expect(placeCard({ x: 600, y: 400, w: 20, h: 20 }, size, viewport)).toEqual({ left: 470, top: 272, side: 'above' })
  })

  it('never crosses the left or right edge', () => {
    expect(placeCard({ x: 2, y: 400, w: 12, h: 12 }, size, viewport).left).toBe(8)
    expect(placeCard({ x: 1275, y: 400, w: 12, h: 12 }, size, viewport).left).toBe(1280 - 8 - 280)
  })

  it('flips below when there is no room above, and stays on screen when neither side fits', () => {
    expect(placeCard({ x: 600, y: 40, w: 20, h: 20 }, size, viewport)).toMatchObject({ side: 'below', top: 68 })
    const tall = placeCard({ x: 100, y: 150, w: 20, h: 20 }, { w: 280, h: 500 }, { w: 390, h: 400 })
    expect(tall.top).toBeGreaterThanOrEqual(8)
    expect(tall.left).toBe(8)
  })
})
