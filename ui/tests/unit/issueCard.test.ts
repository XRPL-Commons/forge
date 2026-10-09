import { describe, it, expect } from 'vitest'
import { completedPath, mergeCompleted, modelCardView, rowCardView } from '../../app/utils/issueCard'
import { ledgerLook } from '../../app/utils/workbench'
import { doneAt, groupIssues, toCard, type CardModel } from '../../app/utils/factory'
import { drawPortrait, portraitKey, type PortraitLook } from '../../app/utils/cube'

const now = 1_000_000

function row(over: Record<string, unknown> = {}): any {
  return {
    key: '1:613', repo: 'XRPL-Commons/academy', repo_id: 1, number: 613, title: 'Fix the quiz timer',
    content_hash: 'x', body_truncated: false, state: 'open', updated_at: new Date((now - 3 * 86400) * 1000).toISOString(),
    labels: [], assignees: [], status: 'attention', summary: { headline: 'The last attempt failed.', plain: 'Tests timed out.' },
    explanation: '', actions: ['retry'], setup_needed: false, environment_blocker: null, pull_requests: [{ number: 70, title: 'Fix', kind: 'candidate' }],
    dependencies: [], fresh: true, revision: 'r', setup_url: '', active: null, attempts: { failed: 2, limit: 3 }, held_by: [],
    ...over,
  }
}

function ledger(over: Record<string, unknown> = {}): any {
  return {
    key: '1:604', repo_id: 1, repo: 'XRPL-Commons/academy', number: 604, title: 'Fix flaky probe',
    state: 'failed', reason: 'Three attempts failed', active: null, failure_count: 2, candidate: { pr: 71 },
    history: [{ kind: 'failed_attempt', at: now - 7200 }], ...over,
  }
}

function view(issues: any[], jobs: any[] = []): any {
  return {
    ok: true, controls_token: 't', active: true, operator: null, repositories: [{ id: 1, name: 'XRPL-Commons/academy', enabled: true }],
    observation: { at: now - 10 }, issues, jobs, runtime: { enabled: true, slots: { build: 2, review: 1, audit: 1 }, cycle_seconds: 30 }, runtime_revision: 'r',
  }
}

describe('ledgerLook', () => {
  it('lights a failed issue amber, worried and scorched, at the stage it reached', () => {
    expect(ledgerLook({ state: 'failed', failure_count: 5, candidate: { pr: 3 } }, null, false)).toEqual({
      stage: 'review', mood: 'worried', accessory: 'magnifier', beacon: true, zzz: false, scorches: 3, confetti: false,
    })
  })

  it('gives a merged issue its bow and confetti', () => {
    const look = ledgerLook({ state: 'merged' }, null, false)
    expect([look.stage, look.mood, look.accessory, look.confetti, look.beacon]).toEqual(['merged', 'happy', 'bow', true, false])
  })

  it('puts a held issue to sleep, and a closed one back in grey with nothing earned', () => {
    expect(ledgerLook({ state: 'hold' }, null, false)).toMatchObject({ stage: 'queued', mood: 'sleep', zzz: true, beacon: false })
    expect(ledgerLook({ state: 'cancelled', candidate: { pr: 2 } }, null, false)).toMatchObject({ stage: 'queued', accessory: null, confetti: false })
  })

  it('follows the job working on it, and hopes while waiting for approval', () => {
    expect(ledgerLook({ state: 'ready' }, { role: 'build' }, true)).toMatchObject({ stage: 'build', mood: 'busy', accessory: 'hat' })
    expect(ledgerLook({ state: 'approval_needed' }, null, false)).toMatchObject({ stage: 'approval', mood: 'hopeful', accessory: 'badge', beacon: true })
  })
})

describe('waiting on other issues', () => {
  it('reads as a calm patient cube with a linked chip, never as needing you', () => {
    const card = rowCardView(row({
      status: 'blocked', explanation: 'Waiting for #90, #101.', summary: null, actions: [], dependencies: [90, 101], attempts: null, pull_requests: [],
    }), now)
    expect(card.status).toEqual({ label: 'Blocked', color: 'neutral', icon: 'i-lucide-link-2', dim: true })
    expect(card.look).toMatchObject({ mood: 'patient', beacon: false, zzz: false })
    expect(portraitKey(card.look)).not.toMatch(/beacon/)
  })

  it('gives an Activity card waiting on dependencies the same calm look', () => {
    expect(ledgerLook({ state: 'ready', blocked_dependencies: [658] }, null, false)).toMatchObject({ mood: 'patient', beacon: false })
    const blocked = ledger({ key: '1:612', number: 612, state: 'ready', failure_count: 0, candidate: null, blocked_dependencies: [658], dependency_reason: 'Waiting for #658.' })
    const card = groupIssues(view([blocked]), now).waiting[0]!
    expect(modelCardView(card, now).status).toEqual({ label: 'Blocked', color: 'neutral', icon: 'i-lucide-link-2', dim: true })
    // A failed issue that also names prerequisites still needs you.
    expect(ledgerLook({ state: 'failed', blocked_dependencies: [658] }, null, false)).toMatchObject({ mood: 'worried', beacon: true })
  })
})

describe('no longer needed', () => {
  it('reads as grey and resting on the Issues page, with an archive chip', () => {
    const card = rowCardView(row({ status: 'dropped', explanation: 'No longer needed: closed as not planned.', summary: null, actions: [], attempts: null }), now)
    expect(card.status).toEqual({ label: 'No longer needed', color: 'neutral', icon: 'i-lucide-archive', dim: true })
    expect(card.look).toMatchObject({ stage: 'queued', mood: 'rest', accessory: null, beacon: false, confetti: false })
  })

  it('shows in Recently completed as No longer needed, not Cancelled or Merged', () => {
    const history = [{ kind: 'cancelled', at: now - 600 }]
    for (const closed_as of ['not_planned', 'duplicate']) {
      const dropped = ledger({ state: 'cancelled', closed_as, candidate: { pr: 71 }, failure_count: 0, history })
      const card = groupIssues(view([dropped]), now).completed[0]!
      expect(card.look).toMatchObject({ stage: 'queued', mood: 'rest', accessory: null, confetti: false, beacon: false })
      expect(modelCardView(card, now).status).toEqual({ label: 'No longer needed', color: 'neutral', icon: 'i-lucide-archive', dim: true })
    }
    const cancelled = groupIssues(view([ledger({ state: 'cancelled', closed_as: 'completed', failure_count: 0, history })]), now).completed[0]!
    expect(modelCardView(cancelled, now).status).toEqual({ label: 'Completed', color: 'neutral' })
    // A merge is never dropped, whatever GitHub says about the close.
    expect(ledgerLook({ state: 'merged', closed_as: 'not_planned' }, null, false)).toMatchObject({ mood: 'happy', confetti: true })
  })
})

describe('portraitKey', () => {
  it('names every prop the frame draws, so a card redraws only when one changes', () => {
    const look: PortraitLook = { stage: 'review', mood: 'worried', accessory: 'magnifier', beacon: true, zzz: false, scorches: 5, confetti: false }
    expect(portraitKey(look)).toBe('review worried magnifier beacon scorch-3')
    expect(portraitKey({ ...look, scorches: 4 })).toBe(portraitKey(look))
    expect(portraitKey({ ...look, beacon: false })).not.toBe(portraitKey(look))
  })
})

describe('drawPortrait', () => {
  // A context that records the calls it receives and accepts any property.
  function recorder() {
    const calls: string[] = []
    const gradient = { addColorStop() {} }
    const ctx = new Proxy({} as Record<string, unknown>, {
      get(target, name: string) {
        if (name in target) return target[name]
        return (..._args: unknown[]) => { calls.push(name); return name === 'createRadialGradient' ? gradient : undefined }
      },
      set(target, name: string, value) { target[name] = value; return true },
    })
    return { ctx: ctx as unknown as CanvasRenderingContext2D, calls }
  }

  it('draws one still frame, with the beacon and z props only when asked', () => {
    const plain: PortraitLook = { stage: 'build', mood: 'open', accessory: 'hat', beacon: false, zzz: false, scorches: 0, confetti: false }
    const quiet = recorder()
    drawPortrait(quiet.ctx, 56, plain)
    const lit = recorder()
    drawPortrait(lit.ctx, 56, { ...plain, beacon: true, zzz: true })
    expect(quiet.calls).not.toContain('fillText')
    expect(quiet.calls).not.toContain('createRadialGradient')
    expect(lit.calls).toContain('fillText')
    expect(lit.calls).toContain('createRadialGradient')
  })
})

describe('rowCardView', () => {
  it('keeps the Issues page facts and badge, and adds hearts and the cube', () => {
    const card = rowCardView(row(), now)
    expect(card.status).toEqual({ label: 'Needs attention', color: 'warning', icon: null, dim: false })
    expect([card.headline, card.plain]).toEqual(['The last attempt failed.', 'Tests timed out.'])
    expect(card.facts.map(fact => [fact.key, fact.text, fact.tone])).toEqual([
      ['pr-70', 'PR #70', 'link'], ['attempts', '2 of 3 attempts failed', 'warn'], ['age', 'Updated 3 d ago', 'muted'],
    ])
    expect(card.facts[1]!.hearts).toEqual({ lost: 2, limit: 3 })
    expect(card.look).toMatchObject({ beacon: true, scorches: 2 })
    expect(card.to).toBe('/issues/1%3A613')
  })

  it('shows no hearts for a large retry limit, only the words', () => {
    expect(rowCardView(row({ attempts: { failed: 2, limit: 8 } }), now).facts.find(fact => fact.key === 'attempts')!.hearts).toBeUndefined()
  })
})

describe('modelCardView', () => {
  it('turns the attempts note into hearts, with the PR and the age', () => {
    const [card] = groupIssues(view([ledger()]), now).attention
    const shown = modelCardView(card!, now)
    expect(shown.status).toEqual({ label: 'Needs attention', color: 'warning' })
    expect(shown.facts.map(fact => [fact.key, fact.text])).toEqual([
      ['pr-71', 'PR #71'], ['attempts', '2 of 3 attempts failed'], ['age', 'Updated 2 h ago'],
    ])
    expect(shown.facts[1]!.hearts).toEqual({ lost: 2, limit: 3 })
    expect(shown.look).toMatchObject({ stage: 'review', mood: 'worried', beacon: true })
  })

  it('says when a completed issue merged or closed', () => {
    const completed = groupIssues(view([
      ledger({ key: '1:606', number: 606, state: 'merged', failure_count: 0, history: [{ kind: 'merged', at: now - 3600 }] }),
      ledger({ key: '1:607', number: 607, state: 'cancelled', failure_count: 0, candidate: null, history: [{ kind: 'cancelled', at: now - 86400 * 2 }] }),
    ]), now).completed
    expect(completed.map(card => modelCardView(card, now).facts.at(-1)!.text)).toEqual(['Merged 1 h ago', 'Closed 2 d ago'])
    expect(modelCardView(completed[0]!, now).look.confetti).toBe(true)
  })

  it('keeps an internal run plain, with its link to the run', () => {
    const job = { id: 'j1', issue_key: '3:900', repo_id: 3, repo: 'x/y', issue_number: 900, role: 'recovery', slot: 'r', provider: 'claude', provider_id: null, model: null, phase: 'running', outcome: null, cleanup: false, fenced: false, created_at: now - 60, deadline: now + 60, started_at: now - 50, observed_at: now - 5 }
    const shown = modelCardView(toCard(job as any, view([], [job]), now), now)
    expect(shown.to).toBe('/issues/3%3A900?job=j1')
    expect(shown.look).toMatchObject({ stage: 'queued', accessory: null, beacon: false })
  })
})

describe('Recently completed paging', () => {
  const card = (key: string, done: number | null) => ({ key, doneAt: done }) as CardModel

  it('keeps one card per issue, newest first, as the server orders them', () => {
    const merged = mergeCompleted([card('1:1', 300), card('1:2', 200)], [card('1:3', 250), card('1:1', 400), card('1:4', null)])
    expect(merged.map(entry => [entry.key, entry.doneAt])).toEqual([['1:1', 400], ['1:3', 250], ['1:2', 200], ['1:4', null]])
  })

  it('asks for ten at a time, from the previous page cursor', () => {
    expect(completedPath(null)).toBe('/api/factory?section=completed&limit=10')
    expect(completedPath('1015:2:15')).toBe('/api/factory?section=completed&limit=10&before=1015%3A2%3A15')
  })

  it('prefers the server completion time to the history tail', () => {
    expect(doneAt(ledger({ state: 'merged', completed_at: 42, history: [{ kind: 'merged', at: 7 }] }))).toBe(42)
    expect(doneAt(ledger({ state: 'merged', history: [{ kind: 'merged', at: 7 }, { kind: 'deployment_checked', at: 9 }] }))).toBe(7)
    expect(doneAt(ledger({ state: 'failed' }))).toBeNull()
  })
})
