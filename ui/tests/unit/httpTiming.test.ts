import { describe, expect, it } from 'vitest'
import { createHttpTimingRecorder } from '../../app/utils/httpTiming'

describe('opt-in HTTP timing records', () => {
  it('keeps real cache classification separate from HTTP status and omits identifiers', () => {
    const samples: object[] = []
    const record = createHttpTimingRecorder(sample => samples.push(sample))
    record('/api/factory?issue=private-repo:123', 304,
      'cache_miss;dur=23.5, projection;dur=23.0, conditional;dur=0.001, request;dur=25.123, secret;dur=123', 39.12345)
    expect(samples).toEqual([{ view: 'factory', status: 304, browserMs: 39.123,
      serverMs: { cache_miss: 23.5, projection: 23, conditional: 0.001, request: 25.123 } }])
    record('/api/delivery?issue=private-repo:123', 304, 'cache_validated;dur=1, request;dur=2', 5)
    expect(samples[1]).toEqual({ view: 'delivery', status: 304, browserMs: 5, serverMs: { cache_validated: 1, request: 2 } })
    record('/api/factory?section=board', 200, 'request;dur=3', 4)
    expect(samples[2]).toEqual({ view: 'board', status: 200, browserMs: 4, serverMs: { request: 3 } })
    record('/api/issues?state=open', 200, 'request;dur=3', 4)
    record('/api/issues?issue=private-repo:123', 200, 'request;dur=3', 4)
    expect(samples.slice(3)).toEqual(['issue_list', 'issue_detail'].map(view => ({
      view, status: 200, browserMs: 4, serverMs: { request: 3 },
    })))
  })

  it('only emits bounded samples with known numeric server timing', () => {
    const samples: object[] = []
    const record = createHttpTimingRecorder(sample => samples.push(sample))
    record('/api/auth', 200, 'request;dur=1', 2)
    record('/api/issues', 200, null, 2)
    record('/api/issues', 200, 'request;dur=NaN, token;desc="private"', 2)
    expect(samples).toEqual([])
    for (let i = 0; i < 150; i++) record('/api/issues', 200, 'cache_hit;dur=0.5, request;dur=1', 2)
    expect(samples).toHaveLength(100)
  })
})
