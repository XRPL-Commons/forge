import { describe, expect, it } from 'vitest'
import { createHttpTimingRecorder, type HttpTimingSample } from '../../app/utils/httpTiming'

describe('opt-in HTTP timing records', () => {
  it('keeps real cache classification separate from HTTP status and omits identifiers', () => {
    const samples: HttpTimingSample[] = []
    const record = createHttpTimingRecorder(samples, 'browser')
    record('/api/factory?issue=private-repo:123', 304,
      'cache_miss;dur=23.5, projection;dur=23.0, conditional;dur=0.001, request;dur=25.123, secret;dur=123', 39.12345)
    expect(samples).toEqual([{ view: 'factory', status: 304, source: 'browser', fetchMs: 39.123,
      serverMs: { cache_miss: 23.5, projection: 23, conditional: 0.001, request: 25.123 } }])
    record('/api/delivery?issue=private-repo:123', 304, 'cache_validated;dur=1, request;dur=2', 5)
    expect(samples[1]).toEqual({ view: 'delivery', status: 304, source: 'browser', fetchMs: 5, serverMs: { cache_validated: 1, request: 2 } })
    record('/api/factory?section=board', 200, 'request;dur=3', 4)
    expect(samples[2]).toEqual({ view: 'board', status: 200, source: 'browser', fetchMs: 4, serverMs: { request: 3 } })
    record('/api/issues?state=open', 200, 'request;dur=3', 4)
    record('/api/issues?issue=private-repo:123', 200, 'request;dur=3', 4)
    expect(samples.slice(3)).toEqual(['issue_list', 'issue_detail'].map(view => ({
      view, status: 200, source: 'browser', fetchMs: 4, serverMs: { request: 3 },
    })))
  })

  it('only emits bounded samples with known numeric server timing', () => {
    const samples: HttpTimingSample[] = []
    const record = createHttpTimingRecorder(samples, 'browser')
    record('/api/auth', 200, 'request;dur=1', 2)
    record('/api/issues', 200, null, 2)
    record('/api/issues', 200, 'request;dur=NaN, token;desc="private"', 2)
    record('/api/issues', 200, 'request;dur=1', NaN)
    record('/api/issues', 200, 'request;dur=1', -1)
    expect(samples).toEqual([])
    for (let i = 0; i < 150; i++) record('/api/issues', 200, 'cache_hit;dur=0.5, request;dur=1', 2)
    expect(samples).toHaveLength(100)
  })

  it('shares the page bound across server and browser recorders, isolating other requests', () => {
    const page: HttpTimingSample[] = []
    const server = createHttpTimingRecorder(page, 'server_render')
    const browser = createHttpTimingRecorder(page, 'browser')
    server('/api/factory?section=board', 200, 'request;dur=1', 2)
    for (let i = 0; i < 150; i++) browser('/api/issues', 304, 'request;dur=1', 3)
    expect(page).toHaveLength(100)
    expect(page[0]).toMatchObject({ source: 'server_render', view: 'board', fetchMs: 2 })
    expect(page.slice(1).every(row => row.source === 'browser')).toBe(true)
    const nextPage: HttpTimingSample[] = []
    createHttpTimingRecorder(nextPage, 'server_render')('/api/issues', 200, 'request;dur=1', 4)
    expect(nextPage).toHaveLength(1)
  })
})
