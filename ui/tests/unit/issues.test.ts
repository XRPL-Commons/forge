import { describe, it, expect } from 'vitest'
import {
  ACTION_EFFECTS, ACTION_LABELS, EVENT_LABELS, SHOW_OPTIONS, STATUS_NAMES,
  decisionNeedsReason, emptyState, freshnessText, listParams, preselectedReviewers,
  reviewRequestText, reviewRows, reviewerOptions, reviewerReason, rowAction,
  type IssueListResponse, type IssueRow,
} from '../../app/utils/issues'

const scope = { id: 1, name: 'XRPL-Commons/academy', complete: true, fresh: true, at: 1790000000, error: null }
const list = (over: Partial<IssueListResponse> = {}): IssueListResponse => ({
  ok: true, controls_token: 'synthetic', issues: [], total: 0, page: 1, pages: 1,
  repositories: [scope], complete: true, actions: [], ...over,
})
const row = (over: Partial<IssueRow> = {}): IssueRow => ({
  key: '1:610', repo: 'XRPL-Commons/academy', repo_id: 1, number: 610, title: 'Make course navigation easier',
  content_hash: 'h', body_truncated: false, state: 'open', updated_at: null, labels: [], assignees: [],
  status: 'backlog', explanation: 'Not queued for Forge.', actions: ['queue'], setup_needed: false,
  environment_blocker: null, pull_requests: [], dependencies: [], fresh: true, revision: 'current',
  setup_url: '/settings/repository?repo_id=1', active: null, ...over,
})

describe('copy maps', () => {
  it('names every status the list and detail can render', () => {
    expect(STATUS_NAMES).toEqual({
      backlog: 'Backlog', queued: 'Queued', working: 'Working',
      attention: 'Needs attention', completed: 'Completed', waiting: 'Waiting',
    })
  })
  it('labels every decision, including stop', () => {
    expect(ACTION_LABELS.queue).toBe('Queue for Forge')
    expect(ACTION_LABELS.backlog).toBe('Return to backlog')
    expect(ACTION_LABELS.pause).toBe('Pause')
    expect(ACTION_LABELS.resume).toBe('Release hold')
    expect(ACTION_LABELS.retry).toBe('Retry attempt')
    expect(ACTION_LABELS.recheck).toBe('Recheck readiness')
    expect(ACTION_LABELS.stop).toBe('Stop attempt')
  })
  it('explains the effect of every decision', () => {
    expect(Object.keys(ACTION_EFFECTS).sort()).toEqual(Object.keys(ACTION_LABELS).sort())
    expect(ACTION_EFFECTS.stop).toBe('This stops the active attempt and discards its unfinished workspace changes. Published pull requests are preserved.')
    expect(ACTION_EFFECTS.backlog).toBe('This removes the issue from Forge’s queue. The GitHub issue stays open.')
  })
  it('labels ledger events for the activity list', () => {
    expect(EVENT_LABELS.discovered).toBe('Added to Forge')
    expect(EVENT_LABELS.operator_conflict).toBe('Decision needs another review')
    expect(EVENT_LABELS.cancelled).toBe('Attempt stopped')
  })
  it('offers every issue view in the Show select, open first', () => {
    expect(SHOW_OPTIONS).toEqual([
      { value: 'open', label: 'Open issues' },
      { value: 'attention', label: 'Needs attention' },
      { value: 'backlog', label: 'Backlog' },
      { value: 'queued', label: 'Queued' },
      { value: 'working', label: 'Working' },
      { value: 'waiting', label: 'Waiting' },
      { value: 'completed', label: 'Completed' },
    ])
  })
})

describe('listParams', () => {
  it('serializes every filter, trimming the search text', () => {
    expect(listParams({ state: 'open', repoId: 2, q: ' logging ', page: 3 })).toBe('state=open&page=3&repo_id=2&q=logging')
  })
  it('omits an absent repository and an empty search', () => {
    expect(listParams({ state: 'completed', repoId: null, q: '   ', page: 1 })).toBe('state=completed&page=1')
  })
  it('escapes a search that would otherwise break the query', () => {
    expect(listParams({ state: 'open', repoId: null, q: 'a&b c', page: 1 })).toBe('state=open&page=1&q=a%26b+c')
  })
})

describe('rowAction', () => {
  it('announces a decision that is still being applied', () => {
    expect(rowAction(row({ status: 'attention' }), { id: 'a1', key: '1:610', status: 'pending', message: null })).toBe('Applying…')
  })
  it('sends a working issue to its progress, an attention issue to a review', () => {
    expect(rowAction(row({ status: 'working' }), null)).toBe('View progress')
    expect(rowAction(row({ status: 'attention' }), null)).toBe('Review')
  })
  it('falls back to viewing the issue', () => {
    expect(rowAction(row({ status: 'backlog' }), null)).toBe('View issue')
    expect(rowAction(row({ status: 'completed' }), null)).toBe('View issue')
  })
})

describe('freshnessText', () => {
  it('reports a scope error verbatim', () => {
    const error = 'Could not finish refreshing. Previously received issues are still shown.'
    expect(freshnessText(list({ repositories: [{ ...scope, error }] }))).toBe(error)
  })
  it('confirms a complete inventory and that viewing is not queuing', () => {
    expect(freshnessText(list())).toBe('Up to date · Viewing issues never queues them for work.')
  })
  it('says an incomplete inventory is still being received', () => {
    expect(freshnessText(list({ complete: false }))).toBe('Showing received issues. The GitHub inventory is still incomplete or needs refreshing.')
  })
  it('says nothing when no repository is configured', () => {
    expect(freshnessText(list({ repositories: [] }))).toBe('')
  })
})

describe('emptyState', () => {
  it('sends an operator with no repositories to repository setup', () => {
    expect(emptyState(list({ repositories: [] }))).toEqual({
      title: 'Choose where Forge should work',
      text: 'Add and enable a repository to see its issues here.',
      link: { to: '/settings/repository', label: 'Add repository →' },
    })
  })
  it('keeps received issues visible while the inventory is incomplete', () => {
    expect(emptyState(list({ complete: false }))).toEqual({
      title: 'Loading the issue list',
      text: 'Previously received issues remain visible while Forge checks GitHub.',
    })
  })
  it('blames the filter when the inventory is complete', () => {
    expect(emptyState(list())).toEqual({ title: 'No issues in this view', text: 'Try another filter.' })
  })
  it('is absent while there are rows to show', () => {
    expect(emptyState(list({ issues: [row()], total: 1 }))).toBeNull()
  })
})

describe('decisionNeedsReason', () => {
  it('requires a written reason for a hold, a retry or a stop', () => {
    expect(decisionNeedsReason('pause')).toBe(true)
    expect(decisionNeedsReason('resume')).toBe(true)
    expect(decisionNeedsReason('retry')).toBe(true)
    expect(decisionNeedsReason('stop')).toBe(true)
  })
  it('does not for a queue, a backlog or a recheck', () => {
    expect(decisionNeedsReason('queue')).toBe(false)
    expect(decisionNeedsReason('backlog')).toBe(false)
    expect(decisionNeedsReason('recheck')).toBe(false)
  })
})

describe('reviewRows', () => {
  it('names the role, the outcome and the lead sentence of each review', () => {
    const rows = reviewRows([
      { role: 'review', outcome: 'passed', summary: 'The change is correct. It adds tests for the redaction helper and keeps the public API stable.' },
      { role: 'audit', outcome: 'changes_requested', summary: 'The install token is still written to the build log.' },
    ])
    expect(rows).toEqual([
      {
        role: 'review', label: 'Review', outcome: 'passed', outcomeLabel: 'Passed',
        lead: 'The change is correct.',
        summary: 'The change is correct. It adds tests for the redaction helper and keeps the public API stable.',
        more: true,
      },
      {
        role: 'audit', label: 'Security review', outcome: 'attention', outcomeLabel: 'Needs attention',
        lead: 'The install token is still written to the build log.',
        summary: 'The install token is still written to the build log.',
        more: false,
      },
    ])
  })

  it('treats any outcome that is not "passed" as needing attention', () => {
    expect(reviewRows([{ role: 'review', outcome: null, summary: 'No verdict' }])[0]).toMatchObject({
      outcome: 'attention', outcomeLabel: 'Needs attention', lead: 'No verdict', more: false,
    })
  })

  it('collapses worker prose to one line, so a lead is never a paragraph fragment', () => {
    const [row] = reviewRows([{ role: 'review', outcome: 'passed', summary: '  Looks good!\n\nOne nit:   the name.  ' }])
    expect(row!.lead).toBe('Looks good!')
    expect(row!.summary).toBe('Looks good! One nit: the name.')
    expect(row!.more).toBe(true)
  })

  it('is empty when the issue has no reviews', () => {
    expect(reviewRows(undefined)).toEqual([])
    expect(reviewRows([])).toEqual([])
  })
})

describe('reviewer control', () => {
  const people = [
    { login: 'maintainer', name: 'A Maintainer' },
    { login: 'second-pair', name: '' },
  ]

  it('offers every collaborator, with the display name when there is one', () => {
    expect(reviewerOptions(people)).toEqual([
      { value: 'maintainer', label: 'maintainer · A Maintainer' },
      { value: 'second-pair', label: 'second-pair' },
    ])
    expect(reviewerOptions(undefined)).toEqual([])
  })

  it('starts from the configured human reviewers this repository actually offers', () => {
    expect(preselectedReviewers({ reviewers: people, default_reviewers: ['Maintainer', 'stranger'] })).toEqual(['Maintainer'])
    expect(preselectedReviewers({ reviewers: [], default_reviewers: ['maintainer'] })).toEqual([])
    expect(preselectedReviewers(null)).toEqual([])
  })

  it('sends the chosen logins as the decision, without duplicates or mentions', () => {
    expect(reviewerReason([' @maintainer ', 'second-pair', 'maintainer', ''])).toBe('maintainer, second-pair')
    expect(reviewerReason([])).toBe('')
  })

  it('says who was asked and how long ago', () => {
    expect(reviewRequestText({ logins: ['a', 'b'], at: 1790000000 }, 1790000720))
      .toBe('Review requested from @a, @b · 12 min ago')
  })

  it('drops the age when there is no clock yet, and says nothing when nobody was asked', () => {
    expect(reviewRequestText({ logins: ['a'], at: 1790000000 }, 0)).toBe('Review requested from @a')
    expect(reviewRequestText(null, 1790000000)).toBe('')
    expect(reviewRequestText({ logins: [], at: 1790000000 }, 1790000000)).toBe('')
  })
})
