import { describe, it, expect } from 'vitest'
import {
  actionPlan, activitySegments, bubbleFor, emptyOutputText, isHeartbeat, liveness, outputText, serverAge, stageTimes, workFacts,
  type BubbleInput, type OutputView,
} from '../../app/utils/liveWork'
import { workbenchLook } from '../../app/utils/workbench'

const BEAT = '{"runner": "alive", "attempt": "a", "at": 100}'
const working: BubbleInput['detail'] = {
  status: 'working', explanation: 'Building', environment_blocker: null, dependencies: [],
  summary: { headline: 'Building', plain: 'Building' },
  operator_status: { code: 'working', label: 'Working', reason_code: 'building', reason: 'Building' },
}
const job = { role: 'build' as const, provider: 'claude' }
const output = (over: Partial<OutputView> = {}): OutputView => ({ ok: true, logs: '', live: true, at: 1000, ...over })

describe('worker output', () => {
  it('hides heartbeat lines unless raw is asked for', () => {
    const view = { logs: `${BEAT}\nbuilding…\n${BEAT}` }
    expect(isHeartbeat(BEAT)).toBe(true)
    expect(isHeartbeat('{"type":"forge.activity"}')).toBe(false)
    expect(outputText(view)).toBe('building…')
    expect(outputText(view, true)).toBe(view.logs)
    // The server's own filtered text wins when present.
    expect(outputText({ logs: 'raw', text: 'clean' })).toBe('clean')
  })

  it('says why there is no output instead of saying nothing', () => {
    expect(emptyOutputText(output({ unavailable: 'runner_unavailable' }))).toMatch(/could not read/)
    expect(emptyOutputText(output({ unavailable: 'not_started' }))).toMatch(/starting/)
    expect(emptyOutputText(output())).toBe('The worker has not printed anything yet.')
    expect(emptyOutputText(null)).toBe('No captured output is available yet.')
  })

  it('counts ages on the server clock, then the browser clock since the read', () => {
    expect(serverAge(990, { at: 1000 }, 50_000, 53_000)).toBe(13)
    expect(serverAge(null, { at: 1000 }, 0, 0)).toBeNull()
    expect(serverAge(990, {}, 0, 0)).toBeNull()
  })
})

describe('liveness', () => {
  it('is alive within a minute of the last heartbeat, stalled after', () => {
    expect(liveness(4)).toEqual({ state: 'alive', text: 'alive 4s ago' })
    expect(liveness(125)).toEqual({ state: 'stalled', text: 'no heartbeat for 2 min' })
    expect(liveness(null).state).toBe('unknown')
  })

  it('puts the cube to sleep, without smoke, when the worker went quiet', () => {
    const row = { number: 5, status: 'working' as const, reason_code: 'build', explanation: 'Building', actions: [], pull_requests: [], dependencies: [] }
    expect(workbenchLook(row).mood).toBe('busy')
    const stalled = workbenchLook(row, { stalled: true })
    expect([stalled.mood, stalled.scene.zzz, stalled.scene.smoke]).toEqual(['sleep', true, false])
    expect(stalled.label).toBe('Cube for #5, Build stage, working, but quiet')
  })
})

describe('bubbleFor', () => {
  it('says what the worker is doing right now, and for how long', () => {
    const bubble = bubbleFor({ detail: working, job, stage: 'build', activityAge: 125, heartbeatAge: 3,
      output: output({ activity: { text: 'Running pnpm build', tool: 'Bash', at: 900 } }) })
    expect(bubble.headline).toBe('Running pnpm build')
    expect(bubble.segments).toEqual([{ text: 'Running ', code: false }, { text: 'pnpm build', code: true }])
    expect(bubble.since).toBe('for 2 min')
    // "Building" is already the badge's reason: never repeated.
    expect(bubble.plain).toBe('')
  })

  it('falls back to a sentence about the stage, as the persona', () => {
    const bubble = bubbleFor({ detail: working, job, stage: 'build', output: null, activityAge: null, heartbeatAge: null })
    expect(bubble.headline).toBe('Ember is building the change')
    expect(bubbleFor({ detail: working, job: { role: 'review', provider: 'codex' }, stage: 'review', output: null, activityAge: null, heartbeatAge: null }).headline)
      .toBe('Bolt is reviewing the pull request')
    expect(bubbleFor({ detail: working, job: null, stage: 'audit', output: output({ unavailable: 'runner_unavailable' }), activityAge: null, heartbeatAge: null }))
      .toMatchObject({ headline: 'Forge is running the security review', plain: expect.stringMatching(/could not read/) })
  })

  it('leads with Forge’s summary of the stage when it says more than the status', () => {
    const summarised = { ...working, summary: { headline: 'Writing the change', plain: 'A worker is writing the code.' } }
    expect(bubbleFor({ detail: summarised, job, stage: 'build', output: null, activityAge: null, heartbeatAge: null }))
      .toMatchObject({ headline: 'Writing the change', plain: 'A worker is writing the code.', segments: null })
  })

  it('says the worker went quiet when heartbeats stop', () => {
    const bubble = bubbleFor({ detail: working, job, stage: 'build', activityAge: 300, heartbeatAge: 200,
      output: output({ activity: { text: 'Running pnpm build', tool: 'Bash', at: 700 } }) })
    expect(bubble).toMatchObject({ headline: 'Ember has gone quiet', stalled: true })
    expect(bubble.plain).toMatch(/No heartbeat for 3 min/)
  })

  it('keeps the summary for other states, without repeating the badge', () => {
    const waiting = { ...working, status: 'waiting' as const, summary: null, explanation: 'Waiting',
      operator_status: { code: 'waiting' as const, label: 'Waiting', reason_code: 'provider', reason: 'Waiting for a coding account' } }
    expect(bubbleFor({ detail: waiting, job: null, stage: 'build', output: null, activityAge: null, heartbeatAge: null }))
      .toMatchObject({ headline: 'Waiting for a coding account', plain: '' })
    const failed = { ...working, status: 'attention' as const, summary: { headline: 'Stopped: the build fails', plain: 'It cannot install.' },
      operator_status: { code: 'attention' as const, label: 'Needs attention', reason_code: 'budget_exhausted', reason: 'Failed' } }
    expect(bubbleFor({ detail: failed, job: null, stage: 'build', output: null, activityAge: null, heartbeatAge: null }))
      .toMatchObject({ headline: 'Stopped: the build fails', plain: 'It cannot install.' })
  })
})

describe('activitySegments', () => {
  it('quotes what the worker typed, not the verb', () => {
    expect(activitySegments('Editing src/foo.ts')).toEqual([{ text: 'Editing ', code: false }, { text: 'src/foo.ts', code: true }])
    expect(activitySegments('Searching the repository for `coach`')).toEqual([{ text: 'Searching the repository for ', code: false }, { text: 'coach', code: true }])
    expect(activitySegments('Reading the saved evidence')).toEqual([{ text: 'Reading the saved evidence', code: false }])
    expect(activitySegments('Thinking it through')).toEqual([{ text: 'Thinking it through', code: false }])
  })
})

describe('workFacts', () => {
  it('names the persona, the model, the attempt and the time left', () => {
    const facts = workFacts({ role: 'build', provider: 'claude', model: 'claude-sonnet-4-6', created_at: 1000, started_at: 1060, deadline: 2860, branch: 'forge/5-build' },
      { failed: 1, limit: 3 }, 1780)
    expect(facts).toEqual({ who: 'Ember', provider: 'claude', model: 'Claude Sonnet 4.6', attempt: 'Build · attempt 2 of 3',
      timing: '12 min in · 18 min left', overdue: false, branch: 'forge/5-build' })
    const late = workFacts({ role: 'review', provider: 'grok', model: null, created_at: 1000, started_at: null, deadline: 1500 }, null, 1600)
    expect([late.who, late.model, late.attempt, late.timing, late.overdue, late.branch]).toEqual(['Nova', 'Grok', 'Review', '10 min in · past its deadline', true, null])
  })
})

describe('stageTimes', () => {
  it('times each stage from its first worker to the next stage’s', () => {
    const times = stageTimes([
      { role: 'review', created_at: 1900, started_at: null },
      { role: 'build', created_at: 1000, started_at: 1010 },
      { role: 'build', created_at: 1500, started_at: null },
      { role: 'recovery', created_at: 1200, started_at: null },
    ])
    expect(times).toEqual({ build: { started: 1010, took: 890 }, review: { started: 1900, took: null } })
    expect(stageTimes(null)).toEqual({})
  })
})

describe('actionPlan', () => {
  const base = { status: 'working' as const, actions: ['pause', 'backlog'] as ('pause' | 'backlog')[], active: 'job', closable: true }
  it('keeps a running issue calm: ask the worker, pause, and ending work behind More', () => {
    expect(actionPlan(base, { setupNeeded: false, internalJob: false, agent: 'Ember' }))
      .toEqual({ primary: null, secondary: ['pause', 'backlog'], ask: 'Ask Ember', destructive: ['stop', 'close'] })
  })

  it('only offers "Discuss blocker" when there is one, and then leads with it', () => {
    const attention = { ...base, status: 'attention' as const, actions: [], active: null }
    expect(actionPlan(attention, { setupNeeded: false, internalJob: false, agent: null }))
      .toEqual({ primary: 'discuss', secondary: [], ask: 'Discuss blocker', destructive: ['close'] })
    expect(actionPlan({ ...base, status: 'waiting', active: null }, { setupNeeded: false, internalJob: false, agent: null }).ask).toBe('Discuss')
  })

  it('leads with approval, then the decision that moves the issue on, then setup', () => {
    const plan = (over: object, setupNeeded = false) => actionPlan({ ...base, status: 'attention', active: null, ...over }, { setupNeeded, internalJob: false, agent: null })
    expect(plan({ approval: { pr: 1, reason: 'x' }, actions: ['assign_reviewer', 'pause'] }).primary).toBe('approve')
    expect(plan({ actions: ['retry', 'pause'] })).toMatchObject({ primary: 'retry', secondary: ['pause'] })
    expect(plan({ actions: [] }, true).primary).toBe('setup')
    expect(actionPlan({ ...base, closable: false, active: null, status: 'waiting' }, { setupNeeded: false, internalJob: true, agent: null }).destructive).toEqual(['reset'])
  })
})
