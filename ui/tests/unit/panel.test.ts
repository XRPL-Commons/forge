import { describe, it, expect } from 'vitest'
import { panelQuery, panelState, panelTarget } from '../../app/utils/panel'
import type { CardModel } from '../../app/utils/factory'

function card(over: Partial<CardModel> = {}): Pick<CardModel, 'key' | 'id' | 'jobId'> {
  return { key: '1:601', id: 'issue-1-601', jobId: null, ...over }
}

describe('panelState', () => {
  it('reads the open issue out of the query', () => {
    expect(panelState({ issue: '1:601' })).toEqual({ key: '1:601', job: null })
  })

  it('reads the run the panel was opened at', () => {
    expect(panelState({ issue: '3:900', job: 'job-33' })).toEqual({ key: '3:900', job: 'job-33' })
  })

  it('takes the first value of a repeated parameter', () => {
    expect(panelState({ issue: ['1:601', '1:602'], job: ['a', 'b'] })).toEqual({ key: '1:601', job: 'a' })
  })

  it('closes the panel for an empty, missing or job-only query', () => {
    expect(panelState({})).toEqual({ key: null, job: null })
    expect(panelState(undefined)).toEqual({ key: null, job: null })
    expect(panelState({ issue: '' })).toEqual({ key: null, job: null })
    expect(panelState({ job: 'job-33' })).toEqual({ key: null, job: null })
  })
})

describe('panelQuery', () => {
  it('opens an issue, keeping the parameters the page already carries', () => {
    expect(panelQuery({ show: 'open' }, { key: '1:601' })).toEqual({ show: 'open', issue: '1:601' })
  })

  it('opens one run of an issue', () => {
    expect(panelQuery({}, { key: '3:900', job: 'job-33' })).toEqual({ issue: '3:900', job: 'job-33' })
  })

  it('never leaves the previous issue’s run behind', () => {
    expect(panelQuery({ issue: '3:900', job: 'job-33' }, { key: '1:601' })).toEqual({ issue: '1:601' })
  })

  it('closes the panel without touching the rest of the query', () => {
    expect(panelQuery({ issue: '1:601', job: 'job-33', show: 'open' }, null)).toEqual({ show: 'open' })
    expect(panelQuery({ show: 'open' }, null)).toEqual({ show: 'open' })
  })

  it('does not mutate the query it was given', () => {
    const query = { issue: '1:601', show: 'open' }
    panelQuery(query, null)
    expect(query).toEqual({ issue: '1:601', show: 'open' })
  })
})

describe('panelTarget', () => {
  it('opens an issue card at the issue alone', () => {
    expect(panelTarget(card({ jobId: 'job-11' }))).toEqual({ key: '1:601', job: null })
  })

  it('opens an internal job card at its own run', () => {
    expect(panelTarget(card({ key: '3:900', id: 'job-33', jobId: 'job-33' }))).toEqual({ key: '3:900', job: 'job-33' })
  })
})
