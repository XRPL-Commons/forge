import { describe, it, expect } from 'vitest'
import { COLLAPSED_LINES, SLOW_SECONDS, SUGGESTED_PROMPTS, clockTime, collapsible, slowNote, elapsedLabel, findingDetail, issueTab, joinedLine, leftLine, liveLine, proposalState, canUndoProposal, nearBottom, presenceMood, replyState, tabQuery, threadEntries, unansweredQuestion, unreadReply, type ThreadMessage } from '../../app/utils/discussion'
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

  it('lets the answer replace the status before it, and offers Try again only on the latest left line', () => {
    const entries = threadEntries([user('a'), left('l1'), answer('b', { instead_of: bolt }), user('c'), left('l2'), user('d'), left('l3')], false)
    // l1 was answered after all: gone. l2's question never was: it stays.
    expect(entries.map(e => e.id)).toEqual(['a', 'b', 'c', 'l2', 'd', 'l3'])
    expect(entries.filter(e => e.kind === 'left').map(e => (e as { actionable: boolean }).actionable)).toEqual([false, true])
    // Not while a reply is on its way, nor when Forge is already finding someone else.
    expect((threadEntries([user('a'), left('l')], true).at(-1) as { actionable: boolean }).actionable).toBe(false)
    expect((threadEntries([user('a'), left('l', { retrying: true })], false).at(-1) as { actionable: boolean }).actionable).toBe(false)
  })
})

describe('the one live line', () => {
  const ember = { provider: 'claude', model: 'claude-sonnet-4-6' }
  it('shows only what the agent is doing now, never the log', () => {
    expect(liveLine(ember, [])).toBe('Ember joined · Claude Sonnet 4.6')
    expect(liveLine(ember, [], { provider: 'codex' })).toBe('Ember joined instead · Claude Sonnet 4.6')
    expect(liveLine(ember, [{ at: 1, text: 'Getting started' }, { at: 2, text: 'Thinking it through' }])).toBe('Ember is thinking it through')
  })
  it('adds a gentle note only past about 90 s', () => {
    expect(SLOW_SECONDS).toBe(90)
    expect(slowNote('code', 89)).toBeNull()
    expect(slowNote('code', 90)).toBe('reading the code, longer than usual')
    expect(slowNote('evidence', 200)).toBe('longer than usual')
  })
})

describe('proposal card', () => {
  const proposal = { action: 'close', reason: 'Blocked by workflow permissions.' }
  it('offers the button until the decision is submitted', () => {
    expect(proposalState({})).toBe('none')
    expect(proposalState({ proposal })).toBe('offer')
    expect(proposalState({ proposal, receipt: 'r' })).toBe('submitted')
  })
  it('does not infer a close or Undo from prose, a request ID, or an old undone marker', () => {
    const message = { proposal, requested: 'close this issue', receipt: 'r', undone: { at: 2 } }
    expect(proposalState(message)).toBe('submitted')
    expect(canUndoProposal(message)).toBe(false)
  })
  it('follows canonical request outcomes and exact reversible authority', () => {
    const action_state = { request_id: 'r', status: 'pending' as const, message: 'Waiting for cleanup', reversible: false, undo: null }
    expect(proposalState({ proposal, action_state })).toBe('pending')
    expect(canUndoProposal({ proposal, action_state })).toBe(false)
    expect(proposalState({ proposal, action_state: { ...action_state, status: 'preparing' } })).toBe('pending')
    expect(proposalState({ proposal, action_state: { ...action_state, status: 'rejected' } })).toBe('failed')
    const confirmed = { ...action_state, status: 'applied' as const }
    expect(proposalState({ proposal, action_state: confirmed })).toBe('closed')
    expect(canUndoProposal({ proposal, action_state: confirmed })).toBe(false)
    expect(canUndoProposal({ proposal, action_state: { ...confirmed, reversible: true } })).toBe(true)
    expect(proposalState({ proposal: { action: 'retry' }, action_state: confirmed })).toBe('applied')
    expect(canUndoProposal({ proposal: { action: 'retry' }, action_state: { ...confirmed, reversible: true } })).toBe(false)
  })
  it('waits for confirmed undo and retains rejected undo separately', () => {
    const action_state = { request_id: 'r', status: 'applied' as const, message: null, reversible: true,
      undo: { request_id: 'u', status: 'pending' as const, message: null } }
    expect(proposalState({ proposal, action_state })).toBe('undo_pending')
    expect(canUndoProposal({ proposal, action_state })).toBe(false)
    expect(proposalState({ proposal, action_state: { ...action_state, undo: { ...action_state.undo, status: 'applied' } } })).toBe('reopened')
    const rejected = { ...action_state, reversible: false, undo: { ...action_state.undo, status: 'rejected' as const, message: 'The closure changed' } }
    expect(proposalState({ proposal, action_state: rejected })).toBe('undo_failed')
    expect(canUndoProposal({ proposal, action_state: rejected })).toBe(false)
    expect(canUndoProposal({ proposal, action_state: { ...rejected, reversible: true } })).toBe(false)
  })
})

describe('collapsible', () => {
  it('keeps a short chat reply open', () => {
    expect(collapsible('The shared type is wrong. Fix the producer first.')).toBe(false)
    expect(collapsible('One.\n\nTwo.\n\n- a\n- b\n- c')).toBe(false)
  })
  it('folds anything past about six lines', () => {
    expect(collapsible(Array.from({ length: 8 }, (_, i) => `Line ${i}`).join('\n'))).toBe(true)
    expect(collapsible('word '.repeat(120))).toBe(true)
    expect(COLLAPSED_LINES).toBe(6)
  })
})

it('suggests a few ways to start', () => {
  expect(SUGGESTED_PROMPTS.length).toBeGreaterThanOrEqual(2)
  expect(SUGGESTED_PROMPTS.length).toBeLessThanOrEqual(3)
})
