import { isFresh, isRunning, readout, type FactoryView } from './factory'

export type Role = 'build' | 'review' | 'audit' | 'merge' | 'recovery'
export const ROLE_LABELS: Record<Role, [string, string]> = { build: ['builder', 'builders'], review: ['reviewer', 'reviewers'], audit: ['auditor', 'auditors'], merge: ['merge worker', 'merge workers'], recovery: ['investigation', 'investigations'] }
// Where each header pill count leads on the Activity page: the factory line
// for the working roles, the investigations line for recovery.
export const ROLE_TARGET: Record<Role, string> = { build: '/#factory-line', review: '/#factory-line', audit: '/#factory-line', merge: '/#factory-line', recovery: '/#runners' }

export interface ActivitySummary { working: Record<Role, number>; queued: number; fresh: boolean }

// The header pill counts by the Activity page's own rules: a worker is
// working when isRunning() says so, and the queue is readout()'s.
export function summarizeActivity(view: FactoryView | null | undefined, now: number): ActivitySummary {
  const working: Record<Role, number> = { build: 0, review: 0, audit: 0, merge: 0, recovery: 0 }
  for (const job of view?.summary?.workers ?? view?.jobs ?? []) {
    if (job.role in working && isRunning(view, job, now)) working[job.role]++
  }
  return { working, queued: readout(view, now)?.queued ?? 0, fresh: isFresh(view, now) }
}
