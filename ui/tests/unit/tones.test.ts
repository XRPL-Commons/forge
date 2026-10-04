import { describe, it, expect } from 'vitest'
import { STATUS_NAMES, type IssueStatus } from '../../app/utils/issues'
import { TONE_BADGE, TONE_TEXT, statusBadge, statusTone } from '../../app/utils/tones'

describe('statusTone', () => {
  it('gives every issue status one badge colour', () => {
    const tones = Object.fromEntries((Object.keys(STATUS_NAMES) as IssueStatus[]).map(status => [status, statusTone(status)]))
    expect(tones).toEqual({
      backlog: 'neutral', queued: 'info', working: 'primary', attention: 'warning', completed: 'primary', waiting: 'neutral',
      blocked: 'neutral', dropped: 'neutral',
    })
  })

  it('gives waiting on other issues a calm chip with a link icon, never amber', () => {
    expect(statusBadge('blocked')).toEqual({ color: 'neutral', icon: 'i-lucide-link-2', dim: true })
    expect(statusBadge('blocked').color).not.toBe(statusTone('attention'))
    expect(statusBadge('waiting')).toEqual({ color: 'neutral', icon: null, dim: false })
    expect(statusBadge('attention')).toEqual({ color: 'warning', icon: null, dim: false })
  })

  it('greys out work that is no longer needed, with an archive icon beside the words', () => {
    expect(statusBadge('dropped')).toEqual({ color: 'neutral', icon: 'i-lucide-archive', dim: true })
    expect(statusBadge('completed').color).toBe('primary')
  })
})

describe('tone maps', () => {
  it('pair each tone with a text class and a badge colour', () => {
    expect(Object.keys(TONE_TEXT).sort()).toEqual(Object.keys(TONE_BADGE).sort())
    expect(TONE_BADGE).toEqual({ ok: 'primary', warn: 'warning', muted: 'neutral' })
  })
})
