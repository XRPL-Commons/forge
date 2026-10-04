import { describe, it, expect } from 'vitest'
import { SUGGESTED_PROMPTS, clockTime, elapsedLabel, findingDetail, issueTab, joinedLine, leftLine, nearBottom, presenceMood, replyState, tabQuery, threadEntries, unansweredQuestion, unreadReply, type ThreadMessage } from '../../app/utils/discussion'
import { panelQuery } from '../../app/utils/panel'
import { issuePath } from '../../app/utils/issues'

describe('issueTab', () => {
  it('opens the discussion only when the URL asks for it', () => {
    expect(issueTab({ tab: 'discussion' })).toBe('discussion')
    expect(issueTab({ tab: ['discussion', 'details'] })).toBe('discussion')
    expect(issueTab({ tab: 'details' })).toBe('details')
    expect(issueTab({ tab: 'nonsense' })).toBe('details')
    expect(issueTab({})).toBe('details')
    expect(issueTab(undefined)).toBe('details')
  })
})

describe('tabQuery', () => {
  it('sets the discussion tab and keeps the rest of the query', () => {
    expect(tabQuery({ issue: '1:610', job: 'j' }, 'discussion')).toEqual({ issue: '1:610', job: 'j', tab: 'discussion' })
  })
  it('leaves Details, the default, out of the URL', () => {
    expect(tabQuery({ issue: '1:610', tab: 'discussion' }, 'details')).toEqual({ issue: '1:610' })
  })
  it('round-trips through issueTab', () => {
    expect(issueTab(tabQuery({}, 'discussion'))).toBe('discussion')
    expect(issueTab(tabQuery({ tab: 'discussion' }, 'details'))).toBe('details')
  })
})

describe('tab links', () => {
  it('a new panel issue starts on Details', () => {
    expect(panelQuery({ issue: '1:610', tab: 'discussion', view: 'x' }, { key: '1:611' })).toEqual({ issue: '1:611', view: 'x' })
  })
  it('the full-page link carries the open tab', () => {
    expect(issuePath('1:610', null, 'discussion')).toBe('/issues/1%3A610?tab=discussion')
    expect(issuePath('1:610', 'job-1', 'discussion')).toBe('/issues/1%3A610?job=job-1&tab=discussion')
    expect(issuePath('1:610', 'job-1', 'details')).toBe('/issues/1%3A610?job=job-1')
    expect(issuePath('1:610')).toBe('/issues/1%3A610')
  })
})

describe('nearBottom', () => {
  it('is true at the end of the thread and within the threshold', () => {
    expect(nearBottom({ scrollTop: 600, clientHeight: 400, scrollHeight: 1000 })).toBe(true)
    expect(nearBottom({ scrollTop: 560, clientHeight: 400, scrollHeight: 1000 })).toBe(true)
  })
  it('is false once the operator scrolled up', () => {
    expect(nearBottom({ scrollTop: 300, clientHeight: 400, scrollHeight: 1000 })).toBe(false)
    expect(nearBottom({ scrollTop: 560, clientHeight: 400, scrollHeight: 1000 }, 10)).toBe(false)
  })
  it('is true when everything fits', () => {
    expect(nearBottom({ scrollTop: 0, clientHeight: 400, scrollHeight: 200 })).toBe(true)
  })
})

describe('replyState', () => {
  it('maps the server statuses', () => {
    expect(replyState('queued')).toBe('replying')
    expect(replyState('running')).toBe('replying')
    expect(replyState('cancelling')).toBe('stopping')
    expect(replyState('cancelled')).toBe('stopped')
    expect(replyState('unavailable')).toBe('failed')
    expect(replyState('idle')).toBe('idle')
    expect(replyState(undefined)).toBe('idle')
  })
})

describe('unreadReply', () => {
  it('is unread only for a new answer while on another tab', () => {
    expect(unreadReply('b', 'a', false)).toBe(true)
    expect(unreadReply('b', 'a', true)).toBe(false)
    expect(unreadReply('a', 'a', false)).toBe(false)
  })
  it('is never unread before the baseline or without an answer', () => {
    expect(unreadReply('a', undefined, false)).toBe(false)
    expect(unreadReply(null, null, false)).toBe(false)
    expect(unreadReply('a', null, false)).toBe(true)
  })
})

describe('unansweredQuestion', () => {
  it('is the last message when it is the operator\'s', () => {
    expect(unansweredQuestion([{ role: 'assistant', text: 'hi' }, { role: 'user', text: 'why?' }])).toBe('why?')
  })
  it('is nothing once the agent answered, or with no thread', () => {
    expect(unansweredQuestion([{ role: 'user', text: 'why?' }, { role: 'assistant', text: 'because' }])).toBeNull()
    expect(unansweredQuestion([])).toBeNull()
  })
})

describe('presence', () => {
  const ember = { provider: 'claude', model: 'claude-sonnet-4-6' }
  const bolt = { provider: 'codex', model: null }
  const user = (id: string, text = 'Why?'): ThreadMessage => ({ id, sequence: 0, role: 'user', text })
  const left = (id: string, extra: Partial<ThreadMessage> = {}): ThreadMessage => ({ id, sequence: 0, role: 'system', event: 'left', reason: 'timeout', text: 'Ember ran out of time after 10 min', agent: ember, ...extra })
  const answer = (id: string, extra: Partial<ThreadMessage> = {}): ThreadMessage => ({ id, sequence: 0, role: 'assistant', text: 'Because', agent: ember, ...extra })

  it('Try again re-asks the question a left line follows', () => {
    expect(unansweredQuestion([user('a'), left('l')])).toBe('Why?')
    expect(unansweredQuestion([user('a'), left('l'), answer('b')])).toBeNull()
  })

  it('reads finding, then joined, then answered, then left as the avatar mood', () => {
    expect(['finding', 'joined', 'answered', 'left'].map(p => presenceMood(p as never))).toEqual(['looking', 'busy', 'happy', 'left'])
  })

  it('says who joined, and when it is someone else instead', () => {
    expect(joinedLine(ember)).toBe('Ember joined · Claude Sonnet 4.6')
    expect(joinedLine(ember, bolt)).toBe('Ember joined instead · Claude Sonnet 4.6')
    expect(joinedLine(bolt)).toBe('Bolt joined · Codex')
  })

  it('names the time a quota frees up, never for a login the operator must fix', () => {
    const until = new Date(2026, 9, 4, 23, 15).getTime() / 1000
    expect(clockTime(until, 'en-GB')).toBe('23:15')
    expect(findingDetail({ phase: 'finding', code: 'blocked_quota', reason: 'Bolt is out of quota', until }, 'en-GB')).toBe('Bolt is out of quota until 23:15')
    expect(findingDetail({ phase: 'finding', code: 'blocked_auth', reason: "Bolt can't sign in", until }, 'en-GB')).toBe("Bolt can't sign in")
    expect(leftLine({ text: 'Ember is out of quota', reason: 'quota', until }, 'en-GB')).toBe('Ember is out of quota until 23:15')
    expect(leftLine({ text: 'You stopped Ember', reason: 'cancelled', until: null })).toBe('You stopped Ember')
  })

  it('counts the wait', () => {
    expect(elapsedLabel(12)).toBe('12 s')
    expect(elapsedLabel(125)).toBe('2 min')
    expect(elapsedLabel(3900)).toBe('1 h 5 min')
    expect(elapsedLabel(-3)).toBe('0 s')
  })

  it('puts a joined line before each answer and offers Try again only on the latest left line', () => {
    const entries = threadEntries([user('a'), left('l1'), answer('b', { instead_of: bolt }), user('c'), left('l2')], false)
    expect(entries.map(e => e.kind)).toEqual(['user', 'left', 'joined', 'answer', 'user', 'left'])
    expect(entries.filter(e => e.kind === 'left').map(e => (e as { actionable: boolean }).actionable)).toEqual([false, true])
    expect(entries[2]).toMatchObject({ kind: 'joined', instead: bolt })
    // Not while a reply is on its way, nor when Forge is already finding someone else.
    expect((threadEntries([user('a'), left('l')], true).at(-1) as { actionable: boolean }).actionable).toBe(false)
    expect((threadEntries([user('a'), left('l', { retrying: true })], false).at(-1) as { actionable: boolean }).actionable).toBe(false)
  })
})

it('suggests a few ways to start', () => {
  expect(SUGGESTED_PROMPTS.length).toBeGreaterThanOrEqual(2)
  expect(SUGGESTED_PROMPTS.length).toBeLessThanOrEqual(3)
})
