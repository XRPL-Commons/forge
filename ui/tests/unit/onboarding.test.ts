import { describe, it, expect } from 'vitest'
import {
  harnessLine, harnessState, harnessSummary, initialStep, repositoryProblem,
  resetComplete, resetStep, signature, step3, technicalChecks, waitingService,
  type Harness, type OnboardingView,
} from '../../app/utils/onboarding'

const now = 1_790_000_000

function role(over: Record<string, unknown> = {}): any {
  return { status: 'ok', registered: true, app_id: 1, install_url: null, ...over }
}

function harness(over: Partial<Harness> = {}): Harness {
  return {
    id: 'codex', name: 'Codex', version: '1.4.0', connected: true,
    models: [{ id: 'codex', model: 'gpt-5-codex', status: 'passed' }],
    login_command: '/opt/forge/bin/forge setup login codex --root /srv/forge',
    check_command: '/opt/forge/bin/forge setup check --root /srv/forge',
    ...over,
  } as Harness
}

function repository(over: Record<string, unknown> = {}): any {
  return { id: 1, name: 'XRPL-Commons/academy', access_ready: true, protection_ready: true, protection_status: 'ready', ...over }
}

// Contracts §2 GET /api/onboarding, the phase-'setup' shape the page boots on.
function view(over: Partial<OnboardingView> = {}): OnboardingView {
  return {
    ok: true, controls_token: 'synthetic', phase: 'setup', generation: 'gen-1', revision: 'rev-2',
    account_type: 'organization', reset_receipt: null,
    roles: { build: role(), review: role(), audit: role(), controller: role() },
    repositories: [repository()], selected: { id: 1, name: 'XRPL-Commons/academy' },
    owner: 'XRPL-Commons', provider_connected: true, checks: {},
    harnesses: [harness()], activity: null, repository_check: null, repository_setup: null,
    qualification_fresh: false, service: { last_seen: now, available: true },
    can_start: false, operation: null, error: null,
    ...over,
  } as OnboardingView
}

function operation(over: Record<string, unknown> = {}): any {
  return { id: 'op-1', action: 'check', generation: 'gen-1', revision: 'rev-2', step: 'probe', at: now, ...over }
}

describe('initialStep', () => {
  it('starts on Start when a repository is selected', () => {
    expect(initialStep(view())).toBe(2)
  })

  it('starts on Repository when every role is ok but nothing is selected', () => {
    expect(initialStep(view({ selected: null }))).toBe(1)
  })

  it('starts on GitHub while a role is not ok', () => {
    const roles = { build: role(), review: role(), audit: role({ status: 'not_configured' }), controller: role() }
    expect(initialStep(view({ selected: null, roles: roles as any }))).toBe(0)
  })
})

describe('harnessLine', () => {
  it('reads "Not connected" when the harness has no sign-in', () => {
    expect(harnessLine(harness({ connected: false, models: [] }))).toBe('Not connected')
  })

  it('reads "Needs verification" when connected without a verified model', () => {
    expect(harnessLine(harness({ models: [] }))).toBe('Needs verification')
  })

  it('drops the suffix when every model passed', () => {
    expect(harnessLine(harness())).toBe('gpt-5-codex')
  })

  it('names the default model when the model id is null', () => {
    expect(harnessLine(harness({ models: [{ id: 'codex', model: null, status: 'passed' }] }))).toBe('Default model')
  })

  it('suffixes every model once one of them is not verified', () => {
    const line = harnessLine(harness({
      models: [
        { id: 'a', model: 'gpt-5-codex', status: 'passed' },
        { id: 'b', model: 'gpt-5-mini', status: 'failed' },
        { id: 'c', model: 'gpt-5-nano', status: 'sign in again' },
      ],
    }))
    expect(line).toBe('gpt-5-codex · Verified\ngpt-5-mini · Verification failed\ngpt-5-nano · Needs verification')
  })
})

describe('harnessState', () => {
  it('is busy while the activity names this harness', () => {
    expect(harnessState(harness(), { target: 'provider', label: 'Checking Codex', provider: 'codex' })).toBe('busy')
  })

  it('ignores an activity for another harness', () => {
    expect(harnessState(harness(), { target: 'provider', label: 'Checking Claude Code', provider: 'claude' })).toBe('verified')
  })

  it('is verified only when every model passed', () => {
    expect(harnessState(harness(), null)).toBe('verified')
    expect(harnessState(harness({ models: [{ id: 'codex', model: null, status: 'failed' }] }), null)).toBe('verify')
    expect(harnessState(harness({ models: [] }), null)).toBe('verify')
  })

  it('asks to connect when the harness has no sign-in', () => {
    expect(harnessState(harness({ connected: false, models: [] }), null)).toBe('connect')
  })
})

describe('harnessSummary', () => {
  it('reports one passing model as verified, the activity label while checking', () => {
    expect(harnessSummary(harness(), null)).toBe('Verified')
    expect(harnessSummary(harness({ models: [{ id: 'codex', model: null, status: 'failed' }] }), null)).toBe('Needs verification')
    expect(harnessSummary(harness({ connected: false, models: [] }), null)).toBe('Not connected')
    expect(harnessSummary(harness(), { target: 'provider', label: 'Checking Codex', provider: 'codex' })).toBe('Checking Codex')
  })
})

describe('step3', () => {
  it('waits for the service when an operation sits unclaimed for more than 15 s', () => {
    const state = view({ operation: operation({ at: now - 20 }), service: { last_seen: now - 20, available: false } })
    const result = step3(state, now)
    expect(result.title).toBe('Waiting for the Forge service')
    expect(result.intro).toBe('Your setup is saved. The service has not picked up this request yet. Check that the Forge supervisor is running on the server.')
    expect(result.primary).toMatchObject({ label: 'Check service setup →', action: 'link', to: '/setup-help#host-installation' })
  })

  it('does not wait before the 15 s threshold', () => {
    const state = view({ operation: operation({ at: now - 5 }), service: { last_seen: now - 5, available: false } })
    expect(waitingService(state, now)).toBe(false)
    expect(step3(state, now).title).toBe('Verifying setup')
  })

  it('reports a running check, and a running start by name', () => {
    const checking = step3(view({ operation: operation() }), now)
    expect(checking.title).toBe('Verifying setup')
    expect(checking.intro).toBe('Forge is checking access, coding accounts and worker cleanup. This does not start issue processing.')
    expect(checking.primary).toBeUndefined()
    expect(checking.status).toEqual({ label: 'Verification in progress… You can leave and return later.', busy: false })

    const starting = step3(view({ operation: operation({ action: 'start' }) }), now)
    expect(starting.title).toBe('Starting Forge')

    const active = step3(view({
      operation: operation(), activity: { target: 'setup', label: 'Preparing verification' },
    }), now)
    expect(active.status).toEqual({ label: 'Preparing verification', busy: true })
  })

  it('offers Start Forge once verification passed', () => {
    const result = step3(view({ can_start: true, checks: { github: 'passed', provider: 'passed', runner: 'passed' } }), now)
    expect(result.title).toBe('Ready to start Forge')
    expect(result.intro).toBe('Verification passed. You decide when issue processing begins.')
    expect(result.primary).toEqual({
      label: 'Start Forge', action: 'start',
      help: 'Starts processing eligible ready issues. Forge creates PRs and merges them when required checks and approvals pass.',
    })
  })

  it('sends the operator to the repository rules when protection is not ready', () => {
    const result = step3(view({ repository_setup: repository({ protection_ready: false }) }), now)
    expect(result.title).toBe('Prepare your repository')
    expect(result.intro).toBe('Check the review requirements and download a ready-to-import ruleset if anything is missing.')
    expect(result.primary).toEqual({
      label: 'Prepare repository →', action: 'link', to: '/settings/repository?repo_id=1',
      help: 'You apply the rules on GitHub. Forge only reads them.',
    })
  })

  it('asks for a coding account before anything else can be verified', () => {
    const result = step3(view({ provider_connected: false }), now)
    expect(result.title).toBe('Connect a coding account')
    expect(result.intro).toBe('Connect at least one coding account before verifying this repository.')
    expect(result.primary).toEqual({ label: 'Connect a coding account', action: 'link', to: '/settings/harnesses' })
  })

  it('explains a failed GitHub check inline and keeps the failed-request links out', () => {
    const result = step3(view({
      error: 'Repository protection is incomplete', checks: { github: 'failed' },
      repository_check: { repo_id: 1, branch: 'main', observed_at: now, available: true, requirements: [] } as any,
    }), now)
    expect(result.title).toBe('Setup needs attention')
    expect(result.intro).toBe('Update the repository settings listed below, then check again. Your coding accounts do not need reconnecting.')
    expect(result.primary).toMatchObject({ label: 'Check again', action: 'check' })
    expect(result.secondary).toBeUndefined()
  })

  it('offers the failed-request link when the failure has no repository report', () => {
    const result = step3(view({ error: 'Worker cleanup could not be verified', checks: { runner: 'failed' } }), now)
    expect(result.title).toBe('Setup needs attention')
    expect(result.intro).toBe('Verification did not finish. Resolve the error above, then check again.')
    expect(result.secondary).toEqual({ label: 'Check GitHub access', action: 'link', to: '/settings/github' })
  })

  it('asks to verify again when a passed check expired', () => {
    const result = step3(view({ checks: { github: 'passed' } }), now)
    expect(result.title).toBe('Verify your repository')
    expect(result.intro).toBe('The previous verification has expired or setup changed. Verify again before starting.')
    expect(result.primary).toMatchObject({ label: 'Verify repository', action: 'check' })
  })

  it('defaults to verifying the repository, with the charge warning until a fresh qualification', () => {
    const result = step3(view(), now)
    expect(result.title).toBe('Verify your repository')
    expect(result.intro).toBe('Your coding accounts are connected. Repository verification is still required before starting.')
    expect(result.primary).toEqual({
      label: 'Verify repository', action: 'check',
      help: 'Verifies accounts and worker isolation. This includes small model requests that may incur provider charges.',
    })
    expect(step3(view({ qualification_fresh: true }), now).primary?.help)
      .toBe('Rechecks repository access and protection using your recent account and worker verification.')
  })
})

describe('resetStep', () => {
  it('names every reset stage, and falls back to verifying', () => {
    expect(resetStep(null)).toBe('Stopping scheduling')
    expect(resetStep(operation({ step: 'queued' }))).toBe('Stopping scheduling')
    expect(resetStep(operation({ step: 'stop_workers' }))).toBe('Stopping workers and verifying cleanup')
    expect(resetStep(operation({ step: 'clear_setup' }))).toBe('Archiving history and clearing setup')
    expect(resetStep(operation({ step: 'probe' }))).toBe('Verifying reset')
  })
})

describe('signature', () => {
  it('ignores the service heartbeat', () => {
    const a = view({ service: { last_seen: now, available: true } })
    const b = view({ service: { last_seen: now + 7, available: true } })
    expect(signature(a, now)).toBe(signature(b, now))
  })

  it('changes when anything the page renders changes', () => {
    expect(signature(view(), now)).not.toBe(signature(view({ can_start: true }), now))
  })

  it('changes when the derived waitingService flag flips', () => {
    const state = view({ operation: operation({ at: now - 20 }), service: { last_seen: now - 20, available: false } })
    expect(waitingService(state, now)).toBe(true)
    expect(waitingService(state, now - 10)).toBe(false)
    expect(signature(state, now)).not.toBe(signature(state, now - 10))
  })
})

describe('technicalChecks', () => {
  it('labels the three checks and reads "Not checked" before an attempt', () => {
    expect(technicalChecks(view())).toEqual([
      { id: 'github', label: 'Repository permissions and protection', value: 'Not checked', busy: false },
      { id: 'provider', label: 'Coding account connections and isolation', value: 'Not checked', busy: false },
      { id: 'runner', label: 'Worker stop, cleanup and reuse', value: 'Not checked', busy: false },
    ])
  })

  it('reads "Not run in this attempt" for a missing check once GitHub failed', () => {
    const rows = technicalChecks(view({ checks: { github: 'failed' } }))
    expect(rows[0].value).toBe('failed')
    expect(rows[1].value).toBe('Not run in this attempt')
  })

  it('waits on a queued check and shows the activity label while it runs', () => {
    expect(technicalChecks(view({ checks: { provider: 'checking' } }))[1].value).toBe('Incomplete')
    expect(technicalChecks(view({ checks: { provider: 'checking' }, operation: operation() }))[1].value).toBe('Waiting')
    const rows = technicalChecks(view({ activity: { target: 'runner', label: 'Checking worker reuse' } }))
    expect(rows[2]).toEqual({ id: 'runner', label: 'Worker stop, cleanup and reuse', value: 'Checking worker reuse', busy: true })
  })
})

describe('repositoryProblem', () => {
  const report = {
    repo_id: 1, branch: 'main', observed_at: now, available: true,
    requirements: [
      { id: 'reviews', label: 'At least two approving reviews', passed: false, detail: 'Require pull requests with at least two approving reviews.', sources: [] },
      { id: 'stale', label: 'Fresh approvals when code changes', passed: true, detail: 'Approvals are dismissed when new commits are pushed.', sources: [] },
    ],
  } as any

  it('is silent unless the GitHub check failed with a report', () => {
    expect(repositoryProblem(view({ repository_check: report }))).toBeNull()
    expect(repositoryProblem(view({ error: 'x', checks: { github: 'passed' }, repository_check: report }))).toBeNull()
  })

  it('counts the failing requirements and keeps the passing ones behind a summary', () => {
    const problem = repositoryProblem(view({ error: 'x', checks: { github: 'failed' }, repository_check: report }))!
    expect(problem.title).toBe('1 repository setting needs attention')
    expect(problem.branch).toBe('Branch: main')
    expect(problem.to).toBe('/settings/repository?repo_id=1')
    expect(problem.missing).toHaveLength(1)
    expect(problem.passedSummary).toBe('1 requirement already passes')
  })

  it('pluralises both counts and reports an unavailable report', () => {
    const two = { ...report, requirements: [...report.requirements, { id: 'readable', label: 'Check repository review rules', passed: false, detail: 'x', sources: [] }] }
    const problem = repositoryProblem(view({ error: 'x', checks: { github: 'failed' }, repository_check: two as any }))!
    expect(problem.title).toBe('2 repository settings need attention')
    const unavailable = repositoryProblem(view({ error: 'x', checks: { github: 'failed' }, repository_check: { ...report, available: false, requirements: [] } as any }))!
    expect(unavailable.title).toBe('Repository protection could not be verified')
    expect(unavailable.passedSummary).toBe('')
  })
})

describe('resetComplete', () => {
  const blank = view({ phase: 'setup', owner: '', selected: null, provider_connected: false, reset_receipt: { id: 'r1', generation: 'gen-0' } })

  it('shows the completion screen on a blank state with a receipt', () => {
    expect(resetComplete(blank, null)).toBe(true)
  })

  it('shows it once the generation changed under this page', () => {
    expect(resetComplete(view({ generation: 'gen-2' }), 'gen-1')).toBe(true)
    expect(resetComplete(view({ generation: 'gen-1' }), 'gen-1')).toBe(false)
  })

  it('withholds it while the reset reported an error, or outside phase setup', () => {
    expect(resetComplete({ ...blank, error: 'Reset could not stop every worker' }, null)).toBe(false)
    expect(resetComplete({ ...blank, phase: 'resetting' }, null)).toBe(false)
  })
})
