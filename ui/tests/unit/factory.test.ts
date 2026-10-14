import { describe, it, expect } from 'vitest'
import {
  destination, elapsed, groupIssues, isFresh, isRunning, jobFor, maxAge,
  readout, deploymentLine, isProgressStatus, jobSummary, phaseLabel, reasonText, toCard, warnings,
} from '../../app/utils/factory'
import { lineAnalyse, lineState } from '../../app/utils/factoryLine'
import { canonical, factoryRow } from './support/rows'

const now = 1_000_000

// Fixture built from contracts §2 (observation.at = now - 10,
// runtime.cycle_seconds = 30, slots {build:2, review:1, audit:1, recovery:1}).
function issue(over: Record<string, unknown> = {}): any {
  return factoryRow({
    key: '1:601', repo_id: 1, repo: 'XRPL-Commons/academy', number: 601,
    title: 'Publish build logs without leaking install tokens',
    state: 'ready', reason: null, requirements: null, candidate: null, active: null,
    failures: [], budget_start: now - 5000, failure_count: 0, retry_plan: null,
    evidence: {}, created_at: now - 5000, history: [], ...over,
  })
}

function job(over: Record<string, unknown> = {}): any {
  return {
    id: '11111111-1111-4111-8111-111111111111', issue_key: '1:601', repo_id: 1,
    repo: 'XRPL-Commons/academy', issue_number: 601, role: 'build', slot: 'build-1',
    provider: 'claude', provider_id: null, model: 'claude-sonnet-4.5',
    publisher: 'forge', branch: 'forge/601', target_branch: 'main',
    runner_config: null, candidate: null, policy: null, requirements: null,
    created_at: now - 200, deadline: now + 1600, phase: 'running', outcome: null,
    cleanup: false, fenced: false, observed_status: 'running', observed_at: now - 10, started_at: now - 180, ...over,
  }
}

function view(over: Record<string, unknown> = {}): any {
  return {
    ok: true, controls_token: 'synthetic', active: true,
    operator: { title: 'Building issue #601', detail: 'Publish build logs', action_url: null, action_label: null },
    repositories: [
      { id: 1, name: 'XRPL-Commons/academy', enabled: true },
      { id: 2, name: 'XRPL-Commons/hackathon', enabled: true },
    ],
    observation: { at: now - 10, errors: [], capacity: null, issues: 0 },
    actions: [], issues: [], jobs: [], slots: {},
    runtime: { enabled: true, slots: { build: 2, review: 1, audit: 1, recovery: 1 }, cycle_seconds: 30 },
    runtime_revision: 'rev', ...over,
  }
}

describe('freshness helpers', () => {
  it('derives maxAge, freshness, the active job and running state', () => {
    expect(maxAge({ cycle_seconds: 30 })).toBe(120)
    expect(maxAge({ cycle_seconds: 300 })).toBe(900)
    expect(isFresh(view(), now)).toBe(true)
    expect(isFresh(view({ observation: { at: now - 500, errors: [] } }), now)).toBe(false)
    const running = job()
    const v = view({ issues: [issue({ active: running.id })], jobs: [running] })
    expect(jobFor(v, v.issues[0])!.id).toBe(running.id)
    expect(isRunning(v, running, now)).toBe(true)
  })
})

describe('phaseLabel', () => {
  it('reports a running job as Working', () => {
    const running = job()
    const v = view({ issues: [issue({ active: running.id })], jobs: [running] })
    expect(phaseLabel(v.issues[0], running, v, now)).toBe('Working')
  })

  it('keeps the main working state separate from observation freshness', () => {
    const stale = job({ observed_status: 'running', observed_at: now - 500, last_observed: now - 500 })
    const v = view({ issues: [issue({ active: stale.id })], jobs: [stale] })
    expect(phaseLabel(v.issues[0], stale, v, now)).toBe('Working')
  })

  it('keeps capacity details in the reason rather than the main state', () => {
    const waiting = job({ capacity_wait_since: now - 30 })
    const v = view({ issues: [issue({ active: waiting.id })], jobs: [waiting] })
    expect(phaseLabel(v.issues[0], waiting, v, now)).toBe('Working')
    expect(phaseLabel(null, waiting, v, now)).toBe('Waiting for memory')
    expect(reasonText(null, waiting)).toContain('Waiting for memory')
  })

  it('falls back to the issue state when there is no job', () => {
    const v = view()
    expect(phaseLabel(issue({ state: 'approval_needed' }), null, v, now)).toBe('Needs attention')
    expect(phaseLabel(issue({ state: 'ready' }), null, v, now)).toBe('Queued')
  })
})

describe('reasonText', () => {
  it('reads the server’s reason for an issue, whatever its job or legacy fields say', () => {
    const waiting = issue({ state: 'ready', reason: 'blocked_auth', operator_status: canonical('queued', 'Waiting for an available coding account', 'provider_unavailable') })
    expect(reasonText(waiting, null)).toBe('Waiting for an available coding account')
    expect(reasonText(waiting, job({ capacity_wait_since: now - 30 }))).toBe('Waiting for an available coding account')
  })

  it('says nothing for a job with no issue unless it waits for memory', () => {
    expect(reasonText(null, job())).toBe('')
    expect(reasonText(null, null)).toBe('')
  })
})

describe('groupIssues', () => {
  const running = job()
  const recovery = job({ id: '33333333-3333-4333-8333-333333333333', issue_key: '3:900', repo_id: 3, repo: 'XRPL-Commons/legacy-tools', issue_number: 900, role: 'recovery', slot: 'recovery-1', model: null })
  const v = view({
    issues: [
      issue({ key: '1:601', number: 601, active: running.id }),
      issue({ key: '1:602', number: 602, state: 'built' }),
      issue({ key: '1:604', number: 604, state: 'failed', reason: 'Three attempts failed', failure_count: 3 }),
      issue({ key: '2:605', number: 605, repo_id: 2, repo: 'XRPL-Commons/hackathon', state: 'audited' }),
      issue({ key: '2:606', number: 606, repo_id: 2, repo: 'XRPL-Commons/hackathon', state: 'merged' }),
      issue({ key: '1:611', number: 611, state: 'ready' }),
    ],
    jobs: [running, recovery],
  })
  const groups = groupIssues(v, now)

  it('places issues in one bucket each, in assignment order', () => {
    expect(groups.columns.build.map(c => c.key)).toEqual(['1:601', '1:611'])
    expect(groups.columns.review.map(c => c.key)).toEqual(['1:602'])
    expect(groups.attention.map(c => c.key)).toEqual(['1:604'])
    expect(groups.finishing.map(c => c.key)).toEqual(['2:605'])
    expect(groups.completed.map(c => c.key)).toEqual(['2:606'])
    const keys = [...groups.columns.build, ...groups.columns.review, ...groups.columns.audit,
      ...groups.attention, ...groups.held, ...groups.finishing, ...groups.investigating, ...groups.completed].map(c => c.key)
    expect(keys.length).toBe(new Set(keys).size)
  })

  it('builds the card model of a working issue', () => {
    const card = groups.columns.build[0]!
    expect(card.id).toBe('issue-1-601')
    expect(card.to).toBe('/issues/1%3A601')
    expect(card.repo).toBe('XRPL-Commons/academy')
    expect(card.number).toBe(601)
    expect(card.phaseLabel).toBe('Working')
    expect(card.providerLine).toBe('Claude Code · claude-sonnet-4.5')
    expect(card.running).toBe(true)
    expect(card.stoppable).toBe(true)
    expect(card.jobId).toBe(running.id)
    expect(card.attempts).toBe(null)
  })

  it('adapts an internal job with no issue row through toCard', () => {
    expect(groups.internalJobs.map(c => c.key)).toEqual(['3:900'])
    const card = groups.internalJobs[0]!
    expect(card).toEqual(toCard(recovery, v, now))
    expect(card.number).toBe(null)
    expect(card.title).toBe('3:900')
    expect(card.to).toBe('/issues/3%3A900?job=' + recovery.id)
    expect(card.jobId).toBe(recovery.id)
    expect(card.stoppable).toBe(true)
  })

  it('keeps an issue in the column of its live job, and moves it once the job is done', () => {
    const live = job({ role: 'build' })
    const built = issue({ key: '1:601', number: 601, state: 'built', active: live.id })
    expect(groupIssues(view({ issues: [built], jobs: [live] }), now).columns.build.map(c => c.key)).toEqual(['1:601'])
    const done = { ...live, outcome: 'accepted', cleanup: true }
    const moved = groupIssues(view({ issues: [{ ...built, active: null }], jobs: [done] }), now)
    expect(moved.columns.build).toEqual([])
    expect(moved.columns.review.map(c => c.key)).toEqual(['1:601'])
  })

  it('lists a recovery job its issue already owns once, as that issue\'s card', () => {
    const investigation = job({ id: 'rec-1', issue_key: '1:604', issue_number: 604, role: 'recovery', slot: 'recovery-1', model: null })
    const owned = groupIssues(view({
      issues: [issue({ key: '1:604', number: 604, state: 'investigating', active: investigation.id })],
      jobs: [investigation],
    }), now)
    expect(owned.investigating.map(c => c.key)).toEqual(['1:604'])
    expect(owned.internalJobs).toEqual([])
  })

  it('stacks held issues apart from the ones waiting on a decision', () => {
    const split = groupIssues(view({ issues: [
      issue({ key: '1:620', number: 620, state: 'hold', reason: 'Paused by an operator' }),
      issue({ key: '1:621', number: 621, state: 'hold', reason: 'Forge environment needs attention (resources)' }),
      issue({ key: '1:622', number: 622, state: 'approval_needed' }),
      issue({ key: '1:623', number: 623, state: 'failed', reason: 'Three attempts failed' }),
    ] }), now)
    expect(split.attention.map(c => c.key)).toEqual(['1:622', '1:623'])
    expect(split.held.map(c => c.key)).toEqual(['1:620', '1:621'])
    expect(split.held[0]!.phaseLabel).toBe('Blocked')
  })

  it('leaves the held group empty when nothing is on hold', () => {
    expect(groups.held).toEqual([])
  })

  it('counts failed attempts against the budget', () => {
    const failed = groupIssues(view({ issues: [
      issue({ key: '2:616', number: 616, repo_id: 2, repo: 'XRPL-Commons/hackathon', state: 'failed', reason: 'Two attempts failed', failure_count: 2 }),
    ] }), now)
    expect(failed.attention[0]!.attempts).toEqual({ failed: 2, limit: 3 })
  })
})

describe('readout', () => {
  const running = job()
  const recovery = job({ id: 'rec-fresh', issue_key: '3:900', role: 'recovery', slot: 'recovery-1', model: null })
  const v = view({
    issues: [
      issue({ key: '1:601', number: 601, active: running.id }),
      issue({ key: '1:602', number: 602, state: 'built' }),
      issue({ key: '1:604', number: 604, state: 'failed', attention_kind: 'failed' }),
      issue({ key: '1:605', number: 605, state: 'hold', reason: 'Forge environment needs attention (resources)', attention_kind: 'env' }),
      issue({ key: '1:607', number: 607, state: 'hold', reason: 'Repeated failure requires a supported change of approach', attention_kind: 'approach' }),
      issue({ key: '1:608', number: 608, state: 'audited' }),
      issue({ key: '2:606', number: 606, repo_id: 2, repo: 'XRPL-Commons/hackathon', state: 'merged', history: [{ kind: 'merged', at: now - 3600 }] }),
      issue({ key: '2:609', number: 609, repo_id: 2, repo: 'XRPL-Commons/hackathon', state: 'merged', history: [{ kind: 'merged', at: now - 3 * 86400 }] }),
      issue({ key: '2:610', number: 610, repo_id: 2, repo: 'XRPL-Commons/hackathon', state: 'merged', history: [{ kind: 'merged', at: now - 30 * 86400 }] }),
    ],
    jobs: [running, recovery],
    observation: { at: now, errors: [{ phase: 'github', reason: 'GitHub refused to merge x/y#1: branch rules still block it' }] },
  })

  it('counts working slots, the queue, merges waiting and merged today', () => {
    // The recovery job is not a worker slot; the merges are 1 h, 3 d and 30 d old.
    expect(readout(v, now)).toEqual({ occupied: 1, working: 1, slots: 4, queued: 1, merging: 1, merged: 1 })
  })

  it('reads zero everywhere on an empty factory, and nothing before the first snapshot', () => {
    expect(readout(view(), now)).toEqual({ occupied: 0, working: 0, slots: 4, queued: 0, merging: 0, merged: 0 })
    expect(readout(null, now)).toBeNull()
  })

  it('agrees with the strip on how many issues need the operator', () => {
    const state = lineState(v, now)!
    // 1:604 failed, 1:605 and 1:607 held for a reason the strip names.
    expect(lineAnalyse(state).attention).toBe(3)
  })
})

describe('warnings', () => {
  const factory = (errors: any[], actions: any[] = []) => view({ observation: { at: now - 10, errors, capacity: null, issues: 0 }, actions })

  it('maps each observation phase to its remediation destination', () => {
    const errors = [
      { issue: '1:824', phase: 'github', reason: 'GitHub observation or publication is unconfirmed' },
      { phase: 'capacity', reason: 'Memory headroom is too low' },
      { phase: 'runner', reason: 'The supervisor has not reported' },
      { phase: 'reconciliation', reason: 'The ledger needs reconciling' },
      { phase: 'sandbox', reason: 'Something else went wrong' },
    ]
    expect(warnings({ factory: factory(errors) }, now, '/')).toEqual([
      { kind: 'github', text: 'GitHub observation or publication is unconfirmed', to: '/settings/github', label: 'Open GitHub access' },
      { kind: 'capacity', text: 'Memory headroom is too low', to: '/settings', label: 'Open worker capacity' },
      { kind: 'runner', text: 'The supervisor has not reported', to: '/setup-help#host-installation', label: 'Check service setup' },
      { kind: 'reconciliation', text: 'The ledger needs reconciling', to: '/setup-help#configuration-and-recovery', label: 'Open help' },
      { kind: 'other', text: 'Something else went wrong', to: '/setup-help', label: 'Open help' },
    ])
  })

  it('never points a warning at the route it is displayed on', () => {
    const out = warnings({ factory: factory([{ phase: 'capacity', reason: 'Memory headroom is too low' }]) }, now, '/settings')
    expect(out[0]).toMatchObject({ to: '/settings#worker-capacity', label: 'Open worker capacity' })
    expect(destination('other', '/setup-help')).toEqual({ to: '', label: '' })
  })

  it('explains missing and invalid planning evidence as distinct failures', () => {
    const errors = [
      { phase: 'planning', reason: 'triage_result_missing' },
      { phase: 'planning', reason: 'triage_result_invalid' },
      { phase: 'planning', reason: 'triage_worker_unsuccessful' },
    ]
    expect(warnings({ factory: factory(errors) }, now, '/').map(item => item.text)).toEqual([
      'The planning worker finished without a result.',
      'The planning worker returned an invalid result.',
      'The planning worker could not complete its proposal.',
    ])
  })

  it('dedupes identical reasons and surfaces rejected actions', () => {
    const errors = [
      { phase: 'github', reason: 'GitHub observation or publication is unconfirmed' },
      { phase: 'github', reason: 'GitHub observation or publication is unconfirmed' },
    ]
    const actions = [
      { id: 'r1', action: 'stop', target: 'job-1', reason: 'Stuck', status: 'rejected', error: 'Worker already stopped' },
      { id: 'r2', action: 'stop', target: 'job-1', reason: 'Stuck', status: 'pending' },
    ]
    const out = warnings({ factory: factory(errors, actions) }, now, '/')
    expect(out).toHaveLength(2)
    expect(out[1]).toEqual({ kind: 'other', text: 'Action could not be applied: Worker already stopped', to: '/setup-help', label: 'Open help' })
  })

  it('surfaces a failed onboarding check as a runner warning', () => {
    const out = warnings({ factory: null, onboarding: { error: 'Sandbox verification failed', checks: { runner: 'failed' } } as any }, now, '/')
    expect(out).toEqual([{ kind: 'runner', text: 'Sandbox verification failed', to: '/settings/harnesses', label: 'Open coding accounts' }])
  })
})

describe('elapsed', () => {
  it('formats seconds, minutes and hours', () => {
    expect(elapsed(0)).toBe('0 sec')
    expect(elapsed(59)).toBe('59 sec')
    expect(elapsed(60)).toBe('1 min')
    expect(elapsed(3599)).toBe('59 min')
    expect(elapsed(3660)).toBe('1h 1m')
  })
})

describe('isProgressStatus', () => {
  it('recognises the busy-on-an-issue titles and nothing else', () => {
    for (const title of ['Building issue #20', 'Reviewing issue #7', 'Auditing issue #1', 'Merging issue #3', 'Investigating issue #9', 'Working on issue #2']) {
      expect(isProgressStatus(title)).toBe(true)
    }
    for (const title of ['Waiting for available memory', 'An issue needs your attention', 'Waiting for a ready issue', null, undefined, '']) {
      expect(isProgressStatus(title)).toBe(false)
    }
  })
})

// ---------------------------------------------------------------------------
// Deployment tracking (spec "Operator UI": the Activity board and the issue
// page read the verdicts Task 4 writes onto the factory view).
// ---------------------------------------------------------------------------

const PROFILE = {
  strategy: 'health',
  deploy: { workflow: '.github/workflows/deploy.yml', environment: 'production', trigger: 'push' },
  domain: 'academy.xrpl-commons.org',
  exposure: { kind: 'health', path: '/health', field: 'commit', header: null },
  workflows_digest: 'sha256:1', confirmed_at: now - 86400, exposure_issue: null,
}

function repositories(profiled: number[]): any[] {
  return [
    { id: 1, name: 'XRPL-Commons/academy', enabled: true },
    { id: 2, name: 'XRPL-Commons/hackathon', enabled: true },
  ].map(repo => (profiled.includes(repo.id)
    ? { ...repo, deployment: { status: 'confirmed', summary: null, proposal: null, profile: PROFILE, observation: null, error: null, reason: null, exposure_issue: null, reread_requested: false } }
    : repo))
}

const merged = (over: Record<string, unknown> = {}) =>
  issue({ state: 'merged', history: [{ kind: 'merged', at: now - 3600 }], deployed: null, deploy_failed: null, ...over })

describe('deployment verdicts', () => {
  const tracked = view({
    repositories: repositories([1]),
    issues: [
      merged({ key: '1:701', number: 701, deployed: { commit: 'abc1234def', at: now - 720, via: 'live' } }),
      merged({ key: '1:702', number: 702 }),
      merged({ key: '1:703', number: 703, deploy_failed: { sha: 'fed4321cba', at: now - 300, run_url: 'https://github.com/x/y/actions/runs/9' } }),
      merged({ key: '2:704', number: 704, repo_id: 2, repo: 'XRPL-Commons/hackathon' }),
    ],
    deployments: { 1: { name: 'XRPL-Commons/academy', live: { commit: 'abc1234def', at: now - 60 }, deploy: null, error: null } },
  })

  it('counts every merge of the day in the readout, deployed or not', () => {
    expect(readout(tracked, now)!.merged).toBe(4)
    expect(readout(view({ repositories: repositories([]), issues: tracked.issues }), now)!.merged).toBe(4)
  })

  it('notes the deployment on completed cards, and only for a tracked repository', () => {
    const completed = Object.fromEntries(groupIssues(tracked, now).completed.map(card => [card.key, card.deployment]))
    expect(completed).toEqual({ '1:701': 'deployed', '1:702': 'waiting for deploy', '1:703': 'deploy failed', '2:704': '' })
  })

  it('writes the issue page line for each of the three states', () => {
    const line = (key: string) => deploymentLine(tracked.issues.find((entry: any) => entry.key === key), tracked, now)
    expect(line('1:701')).toEqual({ text: 'This change was deployed 12 minutes ago, according to the live site.', runUrl: null })
    expect(line('1:702')).toEqual({ text: 'Waiting for this change to be deployed.', runUrl: null })
    expect(line('1:703')).toEqual({ text: 'Deploying this change failed 5 minutes ago.', runUrl: 'https://github.com/x/y/actions/runs/9' })
    expect(line('2:704')).toBe(null)
    expect(deploymentLine(null, tracked, now)).toBe(null)
  })

  it('names the source when the evidence is a GitHub deployment, and omits the age before mount', () => {
    const github = merged({ key: '1:705', deployed: { commit: '0123456789ab', at: now - 30, via: 'github' } })
    expect(deploymentLine(github, tracked, now)!.text).toBe('This change was deployed just now, according to a GitHub deployment.')
    expect(deploymentLine(github, tracked, 0)!.text).toBe('This change was deployed, according to a GitHub deployment.')
  })
})

describe('issues waiting on dependencies', () => {
  const blocked = issue({ key: '1:612', number: 612, blocked_dependencies: [658], operator_status: canonical('blocked', 'Waiting for #658.', 'dependency') })
  const free = issue({ key: '1:613', number: 613 })
  const v = view({ issues: [blocked, free], jobs: [] })

  it('are grouped apart from the stage they would enter, naming the blocker', () => {
    const groups = groupIssues(v, now)
    expect(groups.columns.build.map(c => c.key)).toEqual(['1:613'])
    expect(groups.waiting.map(c => c.key)).toEqual(['1:612'])
    expect(groups.waiting[0]).toMatchObject({ phaseLabel: 'Blocked', reason: 'Waiting for #658.' })
  })

  it('are not counted as queued for a worker', () => {
    expect(readout(v, now)!.queued).toBe(1)
  })

  it('never land in the attention crate or count toward Needs you', () => {
    const groups = groupIssues(v, now)
    expect([...groups.attention, ...groups.held].map(c => c.key)).not.toContain('1:612')
    expect(lineAnalyse(lineState(v, now, groups)!).attention).toBe(0)
  })
})

describe('issues owned by someone else', () => {
  it('keeps ownership blocked while a running worker remains visible', () => {
    const live = job()
    const owned = issue({ active: live.id, operator_status: canonical('blocked', 'Someone else is working on this issue', 'ownership_conflict') })
    const groups = groupIssues(view({ issues: [owned], jobs: [live] }), now)
    // A visible worker does not override the issue's current ownership gate.
    expect(groups.columns.build[0]).toMatchObject({
      phaseLabel: 'Blocked', reason: 'Someone else is working on this issue', running: true, jobId: live.id, stoppable: true,
    })
  })
})

describe('jobSummary', () => {
  const line = (over: Record<string, unknown>) => jobSummary({ role: 'build', phase: 'running', outcome: null, cleanup: false, ...over } as any)

  it('says what a running or starting job is doing', () => {
    expect(line({ role: 'recovery' })).toBe('Investigation · Running')
    expect(line({ phase: 'reserved' })).toBe('Build · Starting')
    expect(line({ phase: 'launching', capacity_wait_since: 5 })).toBe('Build · Waiting for memory')
  })

  it('says a job is cleaning up, and a merge is on GitHub', () => {
    expect(line({ phase: 'stopping', cleanup: true })).toBe('Build · Cleaning up')
    expect(line({ role: 'merge', phase: 'external', cleanup: true })).toBe('Merge · Merging on GitHub')
  })

  it('names a finished job by its outcome, never its phase', () => {
    expect(line({ role: 'audit', phase: 'cleaned', outcome: 'pass' })).toBe('Security audit · Passed')
    expect(line({ role: 'review', phase: 'cleaned', outcome: 'reject' })).toBe('Review · Changes requested')
    expect(line({ phase: 'cleaned', outcome: 'some_new_outcome' })).toBe('Build · Some new outcome')
    expect(line({ phase: 'cleaned' })).toBe('Build · Finished')
  })

  it('never shows a raw phase or role', () => {
    for (const phase of ['reserved', 'created', 'launching', 'running', 'stopping', 'cleaned', 'external']) {
      for (const role of ['build', 'review', 'audit', 'recovery', 'merge']) {
        const [shownRole, shownState] = line({ role, phase }).split(' · ')
        expect(shownRole).not.toBe(role)
        expect(shownState).toMatch(/^[A-Z][^_]*$/)
        expect(['reserved', 'created', 'launching', 'external', 'cleaned']).not.toContain(shownState?.toLowerCase())
      }
    }
  })
})
