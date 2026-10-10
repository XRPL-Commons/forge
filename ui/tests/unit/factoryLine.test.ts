import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import {
  LINE_LOOKS, LINE_RESERVED, LINE_STAGES, lineFill, lineMix, lineAnalyse, lineBlocker, lineDiff, lineGroups, lineHeadline, lineHeadlineText, lineReason, lineState, lineStation,
  lineSummary, lineTooltip, type LineState,
} from '../../app/utils/factoryLine'
import { factoryFreshness, readout } from '../../app/utils/factory'
import { attentionKind } from '../e2e/attention-kind.mjs'

const now = 1_000_000

// Rows carry the server's attention code, as /api/factory sends them.
function issue(number: number, over: Record<string, unknown> = {}): any {
  const row: any = {
    key: `1:${number}`, repo_id: 1, repo: 'XRPL-Commons/academy', number, title: `Issue ${number}`,
    state: 'ready', reason: null, active: null, failure_count: 0, history: [], ...over,
  }
  return 'attention_kind' in over ? row : { ...row, attention_kind: attentionKind(row) }
}

function job(number: number, role: string, over: Record<string, unknown> = {}): any {
  return {
    id: `job-${number}`, issue_key: `1:${number}`, repo_id: 1, repo: 'XRPL-Commons/academy', issue_number: number,
    role, slot: `${role}-1`, provider: 'claude', provider_id: null, model: null, phase: 'running', outcome: null,
    cleanup: false, fenced: false, created_at: now - 200, deadline: now + 1600, observed_status: 'running', observed_at: now - 10, started_at: now - 180, ...over,
  }
}

function view(issues: any[], jobs: any[] = [], over: Record<string, unknown> = {}): any {
  return {
    ok: true, controls_token: 'synthetic', active: true, operator: null,
    repositories: [{ id: 1, name: 'XRPL-Commons/academy', enabled: true }],
    observation: { at: now - 10, errors: [] }, actions: [], issues, jobs,
    runtime: { enabled: true, slots: { build: 2, review: 1, audit: 1, recovery: 1 }, cycle_seconds: 30 },
    runtime_revision: 'rev', ...over,
  }
}

// Build is full (2 of 2) with three queued in front of it; one review
// waits for the free review slot; five issues need the operator.
function busy(): any {
  return view([
    issue(1, { active: 'job-1' }), issue(2, { active: 'job-2' }),
    issue(3), issue(4), issue(5),
    issue(6, { state: 'built' }),
    issue(7, { state: 'reviewed', active: 'job-7' }),
    issue(8, { state: 'audited' }),
    issue(10, { state: 'failed', failure_count: 3, reason: 'Three attempts failed' }),
    issue(11, { state: 'approval_needed', reason: 'A human review is required before merging.' }),
    issue(12, { state: 'hold', reason: 'Forge environment needs attention (toolchain)' }),
    issue(13, { state: 'hold', reason: 'Repeated failure requires a supported change of approach' }),
    issue(14, { state: 'hold', reason: 'Paused by an operator' }),
    issue(15, { state: 'ready', blocked_dependencies: [7], dependency_reason: 'Depends on #7' }),
  ], [job(1, 'build'), job(2, 'build', { phase: 'launching' }), job(7, 'audit')], {
    summary: { merged_day: 4 },
  })
}

function stageOf(state: LineState, id: string) {
  return state.items.find(item => item.id === id)
}

describe('lineState', () => {
  it('maps the board groups onto stations, keyed by issue key', () => {
    const state = lineState(busy(), now)!
    const groups = lineGroups(state.items)
    expect(groups.build.map(item => item.id)).toEqual(['1:1', '1:2'])
    expect(groups.queued.map(item => [item.id, item.next])).toEqual([['1:3', 'build'], ['1:4', 'build'], ['1:5', 'build'], ['1:6', 'review']])
    expect(groups.audit.map(item => item.id)).toEqual(['1:7'])
    expect(groups.approval.map(item => item.id)).toEqual(['1:8'])
    expect(groups.attention.map(item => [item.id, item.reason])).toEqual([['1:13', 'approach'], ['1:11', 'approve'], ['1:10', 'failed'], ['1:12', 'env']])
    expect(groups.hold.map(item => item.id)).toEqual(['1:14'])
    expect(stageOf(state, '1:15')).toMatchObject({ stage: 'waiting', on: [7] })
    expect(state.slots).toEqual({ build: 2, review: 1, audit: 1 })
    expect(state.merged).toBe(4)
  })

  it('marks only a running worker as running', () => {
    const state = lineState(busy(), now)!
    expect(stageOf(state, '1:1')!.running).toBe(true)
    expect(stageOf(state, '1:2')!.running).toBe(false)
  })

  it('keeps canonical status but never animates stale or non-running observations', () => {
    const canonical = { code: 'working', label: 'Working', reason: 'Building the candidate' }
    const item = issue(1, { active: 'job-1', operator_status: canonical })
    for (const snapshot of [
      view([item], [job(1, 'build')], { observation: { at: now - 1000, errors: [] } }),
      view([item], [job(1, 'build', { phase: 'launching' })]),
      view([item], [job(1, 'build', { observed_status: 'running', observed_at: now - 1000 })]),
    ]) {
      const state = lineState(snapshot, now)!
      expect(stageOf(state, '1:1')).toMatchObject({ running: false, status: 'Working' })
      expect(lineAnalyse(state).running).toBe(0)
    }
  })

  it('reads capacities from the runtime, not from constants', () => {
    const state = lineState(view([], [], { runtime: { enabled: true, slots: { build: 5, review: 4, audit: 3 }, cycle_seconds: 30 } }), now)!
    expect(state.slots).toEqual({ build: 5, review: 4, audit: 3 })
    expect(lineAnalyse(state).slots).toBe(12)
  })

  it('is null without a view', () => {
    expect(lineState(null, now)).toBeNull()
  })

  it("sorts issues into the crate by the server's attention code, never by the reason prose", () => {
    for (const kind of ['approach', 'question', 'approve', 'verify', 'failed', 'env', 'review']) {
      expect(lineReason({ attention_kind: kind as any })).toBe(kind)
    }
    expect(lineReason({ attention_kind: null })).toBeNull()
    expect(lineReason({})).toBeNull()
    expect(lineReason({ attention_kind: 'something-new' as any })).toBeNull()
  })
})

describe('lineAnalyse and lineHeadline', () => {
  it('names the fullest stage with work waiting as the bottleneck', () => {
    const info = lineAnalyse(lineState(busy(), now)!)
    expect(info.necks).toEqual([{ stage: 'build', waiting: 3 }])
    expect(info.attention).toBe(4)
    expect(info.reasons).toEqual({ approach: 1, question: 0, approve: 1, verify: 0, failed: 1, env: 1, review: 0 })
    expect(info.occupied).toBe(3)
    expect(info.running).toBe(2)
    expect(lineHeadlineText(lineHeadline(info))).toBe('4 need you · Build is the bottleneck, 3 waiting')
  })

  it('a free slot is not a bottleneck, however long the queue', () => {
    const state = lineState(view([issue(1, { active: 'job-1' }), issue(2), issue(3)], [job(1, 'build')]), now)!
    const info = lineAnalyse(state)
    expect(info.neck).toBeNull()
    expect(lineHeadlineText(lineHeadline(info))).toBe('1 of 4 slots working')
  })

  it('says all quiet when nothing runs and nothing needs the operator', () => {
    const info = lineAnalyse(lineState(view([]), now)!)
    expect(lineHeadline(info)).toEqual([{ t: 'All quiet', c: 'muted' }])
  })

  it('leads with a coding account that is down', () => {
    const info = lineAnalyse(lineState(busy(), now)!)
    expect(lineHeadlineText(lineHeadline(info, 'Codex signed out'))).toBe('Codex signed out · 4 need you · Build is the bottleneck, 3 waiting')
    expect(lineHeadline(lineAnalyse(lineState(view([]), now)!), 'Codex signed out')[0]).toEqual({ t: 'Codex signed out', c: 'coral' })
    expect(lineHeadline(info, null)[0]!.t).toBe('4 need you')
  })

  it('leads with the operator when only attention is left', () => {
    const info = lineAnalyse(lineState(view([issue(1, { state: 'failed', failure_count: 3 })]), now)!)
    expect(lineHeadlineText(lineHeadline(info))).toBe('1 needs you')
  })

  it('summarises the counts for screen readers', () => {
    const state = lineState(busy(), now)!
    expect(lineSummary(state, lineAnalyse(state))).toBe(
      'Queued 4, Build 2 of 2, Review 0 of 1, Audit 1 of 1, waiting to merge 1, merged today 4, need you 4, waiting on other issues 1, on hold 1.',
    )
  })
})

describe('lineTooltip and lineBlocker', () => {
  it('reads like the board', () => {
    const state = lineState(busy(), now)!
    expect(lineTooltip(stageOf(state, '1:10')!, false)).toBe('#10 · Needs attention · An attempt failed')
    expect(lineTooltip(stageOf(state, '1:3')!, true)).toBe('#3 · Queued · Waiting for a worker · XRPL-Commons/academy')
    expect(lineTooltip(stageOf(state, '1:15')!, false)).toBe('#15 · Waiting on other issues · Blocked')
  })

  it('ties a waiting issue to its blocker in the same repository', () => {
    const state = lineState(busy(), now)!
    const byId = new Map(state.items.map(item => [item.id, item]))
    expect(lineBlocker(byId.get('1:15')!, byId)!.id).toBe('1:7')
    expect(lineBlocker(byId.get('1:3')!, byId)).toBeNull()
  })
})

describe('lineDiff', () => {
  it('reports nothing on the first poll', () => {
    expect(lineDiff(null, lineState(busy(), now)!)).toEqual({ entered: [], moved: [], left: [], merged: [], plus: 0 })
  })

  it('reports real moves, arrivals, departures and merges between polls', () => {
    const before = lineState(busy(), now)!
    const after = lineState(view([
      issue(1, { state: 'built' }), issue(2, { active: 'job-2' }), issue(3, { active: 'job-3' }), issue(4), issue(5),
      issue(6, { state: 'built', active: 'job-6' }),
      issue(7, { state: 'reviewed', active: 'job-7' }),
      issue(10, { state: 'failed', failure_count: 3 }), issue(11, { state: 'approval_needed' }),
      issue(12, { state: 'hold', reason: 'Forge environment needs attention (toolchain)' }),
      issue(13, { state: 'hold', reason: 'Repeated failure requires a supported change of approach' }),
      issue(14, { state: 'hold', reason: 'Paused by an operator' }),
      issue(15, { state: 'ready', blocked_dependencies: [7] }),
      issue(20),
    ], [job(2, 'build'), job(3, 'build'), job(6, 'review'), job(7, 'audit')], { summary: { merged_day: 5 } }), now)!
    const diff = lineDiff(before, after)
    expect(diff.entered).toEqual(['1:20'])
    expect(diff.moved).toEqual([
      { id: '1:3', from: 'queued:build', to: 'build' },
      { id: '1:1', from: 'build', to: 'queued:review' },
      { id: '1:6', from: 'queued:review', to: 'review' },
    ])
    expect(diff.merged).toEqual(['1:8'])
    expect(diff.left).toEqual([])
    expect(diff.plus).toBe(1)
  })

  it('fades out an issue that leaves without a merge', () => {
    const before = lineState(view([issue(1), issue(2, { state: 'audited' })]), now)!
    const after = lineState(view([]), now)!
    expect(lineDiff(before, after)).toMatchObject({ left: ['1:1', '1:2'], merged: [], plus: 0 })
  })

  it('places queued issues by the machine they wait for', () => {
    expect(lineStation({ stage: 'queued', next: 'audit' })).toBe('queued:audit')
    expect(lineStation({ stage: 'hold' })).toBe('hold')
  })
})

describe('stage looks', () => {
  const css = readFileSync(`${process.cwd()}/app/assets/css/main.css`, 'utf8')
  const token = (name: string) => css.match(new RegExp(`--color-${name}:\\s*(#[0-9a-f]{6})`, 'i'))![1]!.toLowerCase()
  const reserved = [LINE_RESERVED.amber, LINE_RESERVED.coral, token('amber-200'), token('coral')]
  const hue = (hex: string) => {
    const [r, g, b] = [1, 3, 5].map(i => Number.parseInt(hex.slice(i, i + 2), 16) / 255) as [number, number, number]
    const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min
    if (!d) return null
    const h = max === r ? ((g - b) / d) % 6 : max === g ? (b - r) / d + 2 : (r - g) / d + 4
    return (h * 60 + 360) % 360
  }

  it('gives every stage a colour, and keeps amber and coral for their own jobs', () => {
    expect(token('coral')).toBe(LINE_RESERVED.coral)
    for (const stage of LINE_STAGES) {
      const look = LINE_LOOKS[stage]
      expect(look.fill).toMatch(/^#[0-9a-f]{6}$/)
      expect(look.ready).toMatch(/^#[0-9a-f]{6}$/)
      if (stage === 'attention') continue
      expect(reserved).not.toContain(look.fill)
      expect(reserved).not.toContain(look.ready)
    }
    expect(LINE_LOOKS.attention.fill).toBe(LINE_RESERVED.amber)
  })

  it('matures through cool hues with no reds or oranges', () => {
    for (const stage of ['build', 'review', 'audit'] as const) {
      const h = hue(LINE_LOOKS[stage].fill)!
      expect(h > 60 && h < 300).toBe(true)
    }
    // cyan, then blue, then violet
    expect(hue(LINE_LOOKS.build.fill)!).toBeLessThan(hue(LINE_LOOKS.review.fill)!)
    expect(hue(LINE_LOOKS.review.fill)!).toBeLessThan(hue(LINE_LOOKS.audit.fill)!)
  })

  it('lightens a cube that waits at its machine and tints a waiting cube by where it resumes', () => {
    expect(lineFill({ stage: 'review', running: true })).toBe(LINE_LOOKS.review.fill)
    expect(lineFill({ stage: 'review', running: false })).toBe(LINE_LOOKS.review.ready)
    expect(lineFill({ stage: 'queued', next: 'audit', running: false })).toBe(LINE_LOOKS.queued.fill)
    const tinted = lineFill({ stage: 'waiting', next: 'review', running: false })
    expect(tinted).not.toBe(LINE_LOOKS.waiting.fill)
    expect(tinted).toBe(lineMix(LINE_LOOKS.waiting.fill, LINE_LOOKS.review.fill, 0.28))
    expect(lineFill({ stage: 'waiting', running: false })).toBe(LINE_LOOKS.waiting.fill)
  })

  it('blends colours for a hop', () => {
    expect(lineMix('#000000', '#ffffff', 0)).toBe('#000000')
    expect(lineMix('#000000', '#ffffff', 1)).toBe('#ffffff')
    expect(lineMix('#000000', '#ffffff', 0.5)).toBe('#808080')
    expect(lineMix('#000000', '#ffffff', 2)).toBe('#ffffff')
  })
})


describe('factory freshness wording', () => {
  it('separates heartbeat freshness from a stopped factory and a disconnected page', () => {
    expect(factoryFreshness(view([], []), now)).toBe('Live · checked 10 sec ago')
    expect(factoryFreshness(view([], [], { active: false }), now)).toBe('Factory not running · last known state')
    expect(factoryFreshness(view([], [], { observation: { at: now - 1000 } }), now)).toBe('Updates delayed · last known state')
    expect(factoryFreshness(view([], []), now, true)).toBe('Reconnecting · last known state')
  })
})

describe('worker capacity follows jobs rather than grouped issue cards', () => {
  it('counts one shared owner once and keeps starting slots distinct from running ones', () => {
    const grouped = [issue(1, { active: 'parent' }), issue(2, { active: 'parent' }), issue(3)]
    const parent = job(1, 'build', { id: 'parent', phase: 'reserved', observed_at: null, observed_status: null })
    const snapshot = view(grouped, [parent])
    let state = lineState(snapshot, now)!
    expect(lineAnalyse(state)).toMatchObject({ occupied: 1, running: 0, neck: null })
    expect(lineHeadlineText(lineHeadline(lineAnalyse(state)))).toBe('1 of 4 slots in use')
    expect(readout(snapshot, now)).toMatchObject({ occupied: 1, working: 0 })
    parent.phase = 'running'; parent.observed_at = now - 5; parent.observed_status = 'running'
    state = lineState(snapshot, now)!
    expect(lineAnalyse(state)).toMatchObject({ occupied: 1, running: 1, neck: null })
    expect(readout(snapshot, now)).toMatchObject({ occupied: 1, working: 1 })
  })

  it('counts every leased child in the summary even when only one issue card is shown', () => {
    const workers = [job(1, 'build'), job(2, 'build', { slot: 'build-2' })]
    const snapshot = view([issue(1, { active: 'job-1' })], [workers[0]], { summary: { workers, merged_day: 0 } })
    expect(lineAnalyse(lineState(snapshot, now)!)).toMatchObject({ occupied: 2, running: 2 })
    expect(readout(snapshot, now)).toMatchObject({ occupied: 2, working: 2 })
    workers[1].observed_status = 'paused'
    expect(lineAnalyse(lineState(snapshot, now)!)).toMatchObject({ occupied: 2, running: 1 })
    workers[0].observed_at = now - 1000
    expect(readout(snapshot, now)).toMatchObject({ occupied: 2, working: 0 })
  })
})

// #241: an issue between two stages is the stage settling, never a hold.
describe('transitional gates', () => {
  const working = (reason: string, reasonCode: string) => ({ code: 'working', label: 'Working', reason_code: reasonCode, reason })
  function settling(): any {
    return view([
      issue(1, { state: 'built', transition: { code: 'result_pending', role: 'review', job_id: 'job-r' },
        operator_status: working('Checking the completed worker result', 'result_pending') }),
      issue(2, { state: 'ready', transition: { code: 'cleanup_pending', role: 'build', job_id: 'job-b' },
        operator_status: working('Cleaning up a finished attempt', 'cleanup_pending') }),
      issue(3, { state: 'reviewed', active: 'job-3', operator_status: working('In audit', 'auditing') }),
      issue(4, { state: 'hold', reason: 'Paused by an operator', hold_kind: 'operator_pause' }),
    ], [job(3, 'audit', { phase: 'reserved', observed_status: null, observed_at: null, started_at: null })])
  }

  it('keeps a checking issue in the bay of the job it finished', () => {
    const state = lineState(settling(), now)!
    const groups = lineGroups(state.items)
    expect(groups.review.map(item => [item.id, item.phase, item.running])).toEqual([['1:1', 'checking', false]])
    expect(groups.build.map(item => [item.id, item.phase])).toEqual([['1:2', 'checking']])
    expect(groups.hold.map(item => item.id)).toEqual(['1:4'])
    expect(groups.queued).toEqual([])
    expect(lineTooltip(stageOf(state, '1:1')!, false)).toBe('#1 · Working · Checking the completed worker result')
  })

  it('counts a reserved job toward its bay as starting', () => {
    const state = lineState(settling(), now)!
    const audit = lineGroups(state.items).audit
    expect(audit.map(item => [item.id, item.phase, item.running])).toEqual([['1:3', 'starting', false]])
    expect(lineTooltip(audit[0]!, false)).toBe('#3 · Working · Starting · In audit')
  })

  it('agrees with the readout: settling issues are neither queued nor on hold', () => {
    const v = settling()
    const state = lineState(v, now)!
    expect(readout(v, now)!.queued).toBe(lineGroups(state.items).queued.length)
    expect(lineSummary(state, lineAnalyse(state))).toContain('on hold 1')
  })

  it('ignores a transition naming no bay', () => {
    const state = lineState(view([issue(1, { state: 'built', transition: { code: 'recovery_pending', role: 'recovery', job_id: 'x' } })]), now)!
    expect(lineGroups(state.items).queued.map(item => [item.id, item.next])).toEqual([['1:1', 'review']])
  })
})
