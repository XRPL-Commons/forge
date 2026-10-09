import { computed, shallowRef } from 'vue'
import type { FactoryView, JobView, LedgerIssueView } from './factory'
import type { IssueDetailResponse, IssueRow } from './issues'
import type { Resources } from './resources'

export const BOARD_PATH = '/api/factory?section=board'
export interface FactoryEvent { sequence: number; unit_id: string | null; issue_id: string | null; job_id: string | null; kind: string; at: number }
export interface FactoryChanges { ok: true; cursor: string; at: number; events: FactoryEvent[]; issues: Record<string, IssueRow>; board: FactoryView; resync: boolean }
export interface FactoryTransition { cursor: string; at: number; receivedAt: number; events: FactoryEvent[]; resync: boolean }
export type FactoryPayload = FactoryView | { ok: false; reason: string }
type Read = <T>(path: string, options?: { timeout?: number; signal?: AbortSignal }) => Promise<T>
const same = (left: unknown, right: unknown) => JSON.stringify(left) === JSON.stringify(right)

/** One feed owner per Nuxt app; snapshots and lazy detail share the existing resource cache. */
export class FactoryStore {
  readonly issues = shallowRef(new Map<string, IssueRow>())
  readonly transition = shallowRef<FactoryTransition | null>(null)
  readonly events = computed(() => this.transition.value?.events ?? [])
  private boardIssues = new Map<string, LedgerIssueView>()
  private jobs = new Map<string, JobView>()
  private owners = 0
  private hidden = false
  private active: AbortController | null = null
  private timer?: ReturnType<typeof setTimeout>
  private cursor: string | null = null
  private refreshedDetailsAt: number
  private stopped = true
  private wake = () => { this.active?.abort(); void this.refresh() }
  constructor(readonly resources: Resources, private read: Read, private clock = Date.now) { this.refreshedDetailsAt = clock() }
  get entry() { return this.resources.entry<FactoryPayload>(BOARD_PATH) }
  normalizeBoard(board: FactoryView): FactoryView {
    const issues = board.issues?.map(issue => {
      const old = this.boardIssues.get(issue.key)
      const value = same(old, issue) ? old! : issue
      this.boardIssues.set(issue.key, value); return value
    })
    // Board snapshots are complete for active work, so completed rows need no permanent cache.
    this.boardIssues = new Map((issues ?? []).map(issue => [issue.key, issue]))
    return { ...board, issues, jobs: this.normalizeJobs(board.jobs ?? []) }
  }
  normalizeJobs(jobs: JobView[]): JobView[] {
    return jobs.map(job => {
      const old = this.jobs.get(job.id)
      const value = same(old, job) ? old! : job
      this.jobs.set(job.id, value); return value
    })
  }
  remember(rows: Record<string, IssueRow>) {
    const next = new Map(this.issues.value)
    let changed = false
    for (const [key, value] of Object.entries(rows)) if (!same(next.get(key), value)) { next.set(key, value); changed = true }
    // Retain recent projections, not an unbounded mirror of the server inventory.
    while (next.size > 500) next.delete(next.keys().next().value!)
    if (changed) this.issues.value = next
  }
  rememberDetail(value: IssueDetailResponse) {
    const { body, history, reviews, reviewers, default_reviewers, reviewers_requested, ...summary } = value.issue
    this.remember({ [value.issue.key]: summary })
  }
  async refresh(maxAge = 0) {
    this.active?.abort()
    const value = await this.resources.load<FactoryPayload>(BOARD_PATH, async () => {
      try {
        const result = await this.read<FactoryPayload>(BOARD_PATH)
        return result.ok === false ? result : this.normalizeBoard(result)
      } catch (error) {
        if ((error as { status?: number }).status === 200) return { ok: false, reason: (error as Error).message }
        throw error
      }
    }, maxAge)
    if (value?.ok) this.cursor = value.cursor ?? null
    this.schedule(0)
    return value
  }
  apply(batch: FactoryChanges) {
    this.cursor = batch.cursor
    this.transition.value = { cursor: batch.cursor, at: batch.at, receivedAt: performance.now(), events: batch.events, resync: batch.resync }
    this.remember(batch.issues)
    const board = this.normalizeBoard({ ...batch.board, cursor: batch.cursor })
    this.resources.replace(BOARD_PATH, board)
    // Details contain bodies, review evidence and actions absent from the feed. Reload only
    // mounted detail resources; unopened bodies/artifacts never become feed traffic.
    const keys = new Set(Object.keys(batch.issues))
    // Some GitHub observation fields change without lifecycle events. Busy event traffic
    // must not postpone their refresh indefinitely; the feed owns this one deadline.
    const refreshAll = batch.resync || this.clock() - this.refreshedDetailsAt >= 25000
    if (refreshAll) this.refreshedDetailsAt = this.clock()
    this.resources.invalidate(path => {
      if (!path.startsWith('/api/issues?issue=')) return false
      const key = new URLSearchParams(path.split('?')[1]).get('issue')
      return refreshAll || (!!key && keys.has(key))
    })
    // A resync drops retired projections; active detail bodies remain in the bounded cache.
    if (batch.resync) this.issues.value = new Map(Object.entries(batch.issues))
    if (this.jobs.size > 500) this.jobs = new Map((board.jobs ?? []).map(job => [job.id, job]))
  }
  start() {
    this.owners++
    if (this.owners !== 1) return
    this.stopped = false
    this.entry.listeners.add(this.wake)
    void this.refresh(5000)
  }
  stop() {
    this.owners = Math.max(0, this.owners - 1)
    if (this.owners) return
    this.stopped = true
    clearTimeout(this.timer)
    this.active?.abort()
    this.entry.listeners.delete(this.wake)
  }
  visibility(hidden: boolean) {
    this.hidden = hidden
    if (hidden) { clearTimeout(this.timer); this.active?.abort() }
    else if (!this.stopped) this.schedule(0)
  }
  private schedule(delay: number) {
    clearTimeout(this.timer)
    if (!this.stopped && !this.hidden && !this.active && !this.entry.pending) this.timer = setTimeout(() => void this.poll(), delay)
  }
  private async poll() {
    if (this.stopped || this.hidden || this.active) return
    if (!this.cursor) { await this.refresh(); this.schedule(15000); return }
    const controller = new AbortController()
    this.active = controller
    let delay = 0
    try {
      const batch = await this.read<FactoryChanges>(`/api/factory/changes?since=${encodeURIComponent(this.cursor)}&wait=25`, { timeout: 28000, signal: controller.signal })
      if (!controller.signal.aborted) this.apply(batch)
    } catch (error) {
      if (!controller.signal.aborted) {
        const reason = error instanceof Error ? error.message : 'Live updates are unavailable.'
        if ((error as { status?: number }).status === 200) {
          this.cursor = null
          this.resources.replace(BOARD_PATH, { ok: false, reason })
        } else this.entry.error.value = reason
      }
      delay = controller.signal.aborted ? 0 : 15000
    } finally {
      this.active = null
      this.schedule(delay)
    }
  }
}
