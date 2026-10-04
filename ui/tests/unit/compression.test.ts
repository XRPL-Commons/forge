import { describe, it, expect } from 'vitest'
import { gunzipSync } from 'node:zlib'
import { acceptsGzip, compressJson } from '../../server/utils/compression'

describe('status JSON compression', () => {
  it('honors encoding quality and explicit gzip exclusions', () => {
    for (const value of ['gzip', 'br, gzip;q=0.8', '*;q=1']) expect(acceptsGzip(value)).toBe(true)
    for (const value of ['', 'br', 'gzip;q=0, *;q=1', 'gzip;q=oops', 'gzip;q=2']) expect(acceptsGzip(value)).toBe(false)
  })
  it('round trips large JSON, leaving small bodies and 304 responses alone', async () => {
    const body = Buffer.from(JSON.stringify({ issues: Array(200).fill({ state: 'built', reason: null }) }))
    const compressed = await compressJson(body, 'gzip', 'application/json', 200)
    expect(compressed!.length).toBeLessThan(body.length / 3)
    expect(gunzipSync(compressed!)).toEqual(body)
    for (const [encoding, type, status] of [['gzip;q=0', 'application/json', 200], ['gzip', 'text/html', 200], ['gzip', 'application/json', 304]] as const) {
      expect(await compressJson(body, encoding, type, status)).toBeNull()
    }
    expect(await compressJson(Buffer.from('{}'), 'gzip', 'application/json', 200)).toBeNull()
  })
})
