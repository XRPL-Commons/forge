import { describe, it, expect } from 'vitest'
import { SUGGESTED_PROMPTS, issueTab, nearBottom, replyState, tabQuery, unansweredQuestion, unreadReply } from '../../app/utils/discussion'
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

it('suggests a few ways to start', () => {
  expect(SUGGESTED_PROMPTS.length).toBeGreaterThanOrEqual(2)
  expect(SUGGESTED_PROMPTS.length).toBeLessThanOrEqual(3)
})
