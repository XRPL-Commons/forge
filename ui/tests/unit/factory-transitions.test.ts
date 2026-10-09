import { describe, expect, it } from 'vitest'
import { lineTransition } from '../../app/utils/factoryLine'
import type { FactoryTransition } from '../../app/utils/factoryStore'
const frame = (extra = {}): FactoryTransition => ({ cursor: 'instance:2', at: 100, receivedAt: 10000,
  events: [{ sequence: 2, kind: 'phase_changed', at: 99.75, unit_id: 'unit', issue_id: '1:1', job_id: null }], resync: false, ...extra })
describe('committed factory motion', () => {
  it('uses server event age and local render delay for the animation origin', () => {
    expect(lineTransition(frame(), 'instance:2', 'instance:1', 10100)).toEqual({ cursor: 'instance:2', animate: true, at: 9750 })
  })
  it('does not replay a consumed sequence or animate a different snapshot', () => {
    expect(lineTransition(frame(), 'instance:2', 'instance:2', 10100)).toBeNull()
    expect(lineTransition(frame(), 'instance:3', 'instance:1', 10100)).toBeNull()
    expect(lineTransition(null, 'instance:2', null, 10100)).toBeNull()
  })
  it('snaps resync, idle and old catch-up snapshots instead of inventing transitions', () => {
    expect(lineTransition(frame({ resync: true }), 'instance:2', null, 10100)?.animate).toBe(false)
    expect(lineTransition(frame({ events: [] }), 'instance:2', null, 10100)?.animate).toBe(false)
    expect(lineTransition(frame({ at: 110 }), 'instance:2', null, 10100)?.animate).toBe(false)
    const events = [{ ...frame().events[0]!, sequence: 1, at: 90 }, ...frame().events]
    expect(lineTransition(frame({ events }), 'instance:2', null, 10100)?.animate).toBe(false)
  })
})
