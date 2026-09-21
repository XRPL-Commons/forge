import { describe, it, expect } from 'vitest'
import { formatAge, formatDuration, clampTitle } from '../../app/utils/format'
describe('format', () => {
  it('ages', () => { expect(formatAge(5)).toBe('just now'); expect(formatAge(130)).toBe('2 min'); expect(formatAge(7200)).toBe('2 h'); expect(formatAge(200000)).toBe('2 d') })
  it('durations', () => { expect(formatDuration(59)).toBe('less than a minute'); expect(formatDuration(60)).toBe('1 min'); expect(formatDuration(3900)).toBe('1 h 05 min') })
  it('clamps titles at a word boundary with an ellipsis', () => {
    expect(clampTitle('short')).toBe('short')
    const long = 'word '.repeat(40).trim(); const out = clampTitle(long, 30)
    expect(out.length).toBeLessThanOrEqual(31); expect(out.endsWith('…')).toBe(true)
  })
})
