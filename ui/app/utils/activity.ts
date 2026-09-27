export type Role = 'build' | 'review' | 'audit' | 'merge' | 'recovery'
export const ROLE_LABELS: Record<Role, [string, string]> = { build: ['builder', 'builders'], review: ['reviewer', 'reviewers'], audit: ['auditor', 'auditors'], merge: ['merge worker', 'merge workers'], recovery: ['investigation', 'investigations'] }
// Where each header pill count leads on the Activity page: the section that
// holds that role's cards (merge work is grouped under Finishing).
export const ROLE_TARGET: Record<Role, string> = { build: '/#stage-build', review: '/#stage-review', audit: '/#stage-audit', merge: '/#finishing', recovery: '/#runners' }
export function summarizeActivity(payload: any, now: number) {
  const maxAge = Math.max(120, (Number(payload?.runtime?.cycle_seconds) || 30) * 3)
  const fresh = !!payload?.observation?.at && now - payload.observation.at <= maxAge
  const working: Record<Role, number> = { build: 0, review: 0, audit: 0, merge: 0, recovery: 0 }
  for (const job of payload?.jobs ?? []) {
    const seen = job.observed_at ?? job.last_observed
    if (job.cleanup || job.fenced || job.outcome || job.phase !== 'running' || !seen || now - seen > maxAge) continue
    if (job.role in working) working[job.role as Role]++
  }
  const repositories = Array.isArray(payload?.repositories) ? payload.repositories : []
  const enabled = new Set(repositories.filter((r: any) => r.enabled).map((r: any) => r.name))
  const queued = (payload?.issues ?? []).filter((i: any) => !i.active && ['ready', 'built', 'reviewed'].includes(i.state) && !i.blocked_dependencies?.length && (!Array.isArray(payload?.repositories) || enabled.has(i.repo))).length
  return { working, queued, fresh }
}
