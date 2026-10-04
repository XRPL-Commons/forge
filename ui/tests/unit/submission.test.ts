import { describe, expect, it } from 'vitest'
import { useSubmission } from '../../app/composables/useSubmission'

describe('useSubmission', () => {
  it('runs one task at a time and reports pending', async () => {
    const submission = useSubmission()
    let release!: () => void
    let calls = 0
    const task = () => { calls += 1; return new Promise<void>((resolve) => { release = resolve }) }
    const first = submission.run(task)
    expect(submission.pending.value).toBe(true)
    expect(await submission.run(task)).toBe(false)
    expect(calls).toBe(1)
    release()
    expect(await first).toBe(true)
    expect(submission.pending.value).toBe(false)
    expect(submission.error.value).toBeNull()
  })

  it('keeps the error and re-enables after a failure', async () => {
    const submission = useSubmission('fallback')
    expect(await submission.run(async () => { throw new Error('Refused: stale revision') })).toBe(false)
    expect(submission.error.value).toBe('Refused: stale revision')
    expect(submission.pending.value).toBe(false)
    expect(await submission.run(async () => { throw 'odd' })).toBe(false)
    expect(submission.error.value).toBe('fallback')
    submission.reset()
    expect(submission.error.value).toBeNull()
  })
})
