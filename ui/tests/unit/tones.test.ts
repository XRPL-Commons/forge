import { describe, it, expect } from 'vitest'
import { STATUS_NAMES, type IssueStatus } from '../../app/utils/issues'
import { TONE_BADGE, TONE_TEXT, statusTone } from '../../app/utils/tones'

describe('statusTone', () => {
  it('gives every issue status one badge colour', () => {
    const tones = Object.fromEntries((Object.keys(STATUS_NAMES) as IssueStatus[]).map(status => [status, statusTone(status)]))
    expect(tones).toEqual({
      backlog: 'neutral', queued: 'info', working: 'primary', attention: 'warning', completed: 'primary', waiting: 'neutral',
    })
  })
})

describe('tone maps', () => {
  it('pair each tone with a text class and a badge colour', () => {
    expect(Object.keys(TONE_TEXT).sort()).toEqual(Object.keys(TONE_BADGE).sort())
    expect(TONE_BADGE).toEqual({ ok: 'primary', warn: 'warning', muted: 'neutral' })
  })
})
