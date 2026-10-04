import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { workbenchLook, type WorkbenchRow } from '../../app/utils/workbench'
import { STAGE_FILL } from '../../app/utils/cube'
import { LINE_LOOKS, LINE_RESERVED } from '../../app/utils/factoryLine'

const base: WorkbenchRow = { number: 50, status: 'queued', explanation: 'Queued for Forge.', actions: [], pull_requests: [], dependencies: [] }
const look = (over: Partial<WorkbenchRow>) => workbenchLook({ ...base, ...over })
const candidate = [{ number: 67, title: 'Fix', kind: 'candidate' as const }]
const passed = (role: 'review' | 'audit') => ({ role, outcome: 'pass', summary: 'PASS.' })

describe('workbenchLook', () => {
  it('rests a queued issue in grey, with nothing earned yet', () => {
    const view = look({})
    expect(view.stage).toBe('queued')
    expect(view.mood).toBe('open')
    expect(view.needsYou).toBe(false)
    expect(view.scene.accessory).toBeNull()
    expect(view.badges.map(badge => badge.state)).toEqual(['future', 'future', 'future', 'future', 'future'])
    expect(view.label).toBe('Cube for #50, Queue stage, waiting')
  })

  it('puts a hard hat on a running build, with smoke from the bench', () => {
    const view = look({ status: 'working', reason_code: 'build', explanation: 'Building', attempts: { failed: 0, limit: 3 } })
    expect([view.stage, view.mood, view.scene.accessory]).toEqual(['build', 'busy', 'hat'])
    expect(view.scene.smoke).toBe(true)
    expect(view.scene.beacon).toBe(false)
    expect(view.badges.map(badge => badge.state)).toEqual(['current', 'future', 'future', 'future', 'future'])
    expect(view.attempts).toEqual({ lost: 0, left: 3, limit: 3, text: 'attempt 1 of 3' })
    expect(view.label).toBe('Cube for #50, Build stage, working')
  })

  it('gives a reviewer a magnifier and no smoke', () => {
    const view = look({ status: 'working', reason_code: 'review', explanation: 'Reviewing', pull_requests: candidate })
    expect([view.stage, view.scene.accessory, view.scene.smoke]).toEqual(['review', 'magnifier', false])
    expect(view.badges.map(badge => badge.state)).toEqual(['done', 'current', 'future', 'future', 'future'])
  })

  it('worries, scorched and lit amber, when every attempt failed at the security review', () => {
    const view = look({
      status: 'attention', explanation: 'The last attempt failed.', actions: ['retry'], pull_requests: candidate,
      reviews: [passed('review'), { role: 'audit', outcome: 'fail', summary: 'Fee check.' }],
      attempts: { failed: 3, limit: 3 },
    })
    expect([view.stage, view.mood, view.needsYou]).toEqual(['audit', 'worried', true])
    expect(view.scene.scorches).toBe(3)
    expect(view.scene.beacon).toBe(true)
    expect(view.attempts).toEqual({ lost: 3, left: 0, limit: 3, text: '0 of 3 left' })
    expect(view.label).toBe('Cube for #50, Security stage, needs attention')
  })

  it('counts lives against the limit the server sends, never past it', () => {
    expect(look({ status: 'attention', actions: ['retry'], attempts: { failed: 2, limit: 5 } }).attempts)
      .toEqual({ lost: 2, left: 3, limit: 5, text: '3 of 5 left' })
    expect(look({ status: 'attention', actions: ['retry'], attempts: { failed: 7, limit: 3 } }).scene.scorches).toBe(3)
    expect(look({ attempts: null }).attempts).toBeNull()
  })

  it('wears the rosette and hopes while it waits for a human approval', () => {
    const view = look({
      status: 'attention', explanation: 'The pull request needs a human review.', actions: ['assign_reviewer'],
      approval: { pr: 31, reason: 'A person must sign off.' }, pull_requests: candidate,
      reviews: [passed('review'), passed('audit')],
    })
    expect([view.stage, view.mood, view.scene.accessory]).toEqual(['approval', 'hopeful', 'badge'])
    expect(view.needsYou).toBe(true)
    expect(view.badges.map(badge => badge.state)).toEqual(['done', 'done', 'done', 'current', 'future'])
    expect(view.label).toBe('Cube for #50, Approval stage, waiting for your approval')
  })

  it('worries instead when the merge itself is blocked', () => {
    const view = look({ status: 'attention', merge_blocker: { reason: 'Branch rules' }, pull_requests: candidate })
    expect([view.stage, view.mood]).toEqual(['approval', 'worried'])
  })

  it('sleeps on a hold, with z’s', () => {
    const paused = look({ status: 'attention', explanation: 'Paused by an operator.', actions: ['resume'] })
    expect([paused.mood, paused.scene.zzz]).toEqual(['sleep', true])
    expect(paused.label).toBe('Cube for #50, Queue stage, on hold')
    const labelled = look({ status: 'attention', explanation: 'Paused by hold on GitHub.', actions: ['resume'], held_by: [{ label: 'hold', login: 'x', at: '' }] })
    expect(labelled.mood).toBe('sleep')
  })

  it('ties a ghost cube to each dependency', () => {
    expect(look({ status: 'queued', dependencies: [85, 86] }).scene.dependencies).toEqual([85, 86])
  })

  it('waits patiently, tethered to the open prerequisites and never lit, when blocked on other issues', () => {
    const view = look({ status: 'blocked', explanation: 'Waiting for #90, #101.', dependencies: [85, 90, 101] })
    expect([view.mood, view.needsYou, view.scene.beacon, view.scene.zzz]).toEqual(['patient', false, false, false])
    expect(view.scene.dependencies).toEqual([90, 101])
    expect(view.label).toBe('Cube for #50, Queue stage, waiting on other issues')
    // Without the numbers in the explanation, the body's dependencies stand in.
    expect(look({ status: 'blocked', explanation: '', dependencies: [85] }).scene.dependencies).toEqual([85])
  })

  it('rests in grey, eyes closed, with no bow or confetti, when no longer needed', () => {
    const view = look({ status: 'dropped', explanation: 'No longer needed: closed as a duplicate.', pull_requests: candidate })
    expect([view.stage, view.mood, view.needsYou]).toEqual(['queued', 'rest', false])
    expect([view.scene.accessory, view.scene.confetti, view.scene.beacon, view.scene.zzz]).toEqual([null, false, false, false])
    expect(view.label).toBe('Cube for #50, Queue stage, no longer needed')
  })

  it('celebrates a merge with a bow, confetti and every badge earned', () => {
    const view = look({ status: 'completed', reason_code: 'merged', explanation: 'Completed and merged.', pull_requests: candidate })
    expect([view.stage, view.mood, view.scene.accessory, view.scene.confetti]).toEqual(['merged', 'happy', 'bow', true])
    expect(view.badges.every(badge => badge.state === 'done')).toBe(true)
    expect(view.label).toBe('Cube for #50, Merged stage, merged')
  })

  it('leaves an issue closed without merging grey and at rest', () => {
    const view = look({ status: 'completed', reason_code: 'closed', explanation: 'Closed on GitHub.' })
    expect([view.stage, view.mood, view.scene.confetti]).toEqual(['queued', 'open', false])
    expect(view.label).toBe('Cube for #50, Queue stage, closed')
  })
})

describe('stage palette', () => {
  it('is the factory line’s, and never the amber or coral kept for attention', () => {
    for (const [stage, fill] of Object.entries(STAGE_FILL)) {
      expect(fill).toBe(LINE_LOOKS[stage as keyof typeof STAGE_FILL].fill)
      expect([LINE_RESERVED.amber, LINE_RESERVED.coral]).not.toContain(fill)
    }
  })

  it('matches the --color-stage-* tokens in main.css', () => {
    const css = readFileSync(`${process.cwd()}/app/assets/css/main.css`, 'utf8')
    for (const [stage, fill] of Object.entries(STAGE_FILL)) expect(css).toContain(`--color-stage-${stage}: ${fill};`)
  })
})
