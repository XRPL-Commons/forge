// How much each issue unblocks: the number of issues that wait on it,
// directly or through a chain of other waiting issues. Computed client-side
// from the factory rows' `blocked_dependencies`, which name open issues of
// the same repository by number. An issue never counts itself, and a cycle
// of dependencies is walked once.
import type { LedgerIssueView } from './factory'

export type DependencyRow = Pick<LedgerIssueView, 'key' | 'repo_id' | 'number'> & { blocked_dependencies?: number[] | null }

const at = (repo: number, number: number) => `${repo}#${number}`

// Issue key -> how many other issues transitively wait on it. Every row is
// present, with 0 when nothing waits on it.
export function unblockCounts(rows: readonly DependencyRow[]): Map<string, number> {
  const keyAt = new Map<string, string>()
  for (const row of rows) keyAt.set(at(row.repo_id, row.number), row.key)
  // blocker key -> the keys that wait on it directly
  const waiters = new Map<string, string[]>()
  for (const row of rows) {
    for (const number of row.blocked_dependencies ?? []) {
      const blocker = keyAt.get(at(row.repo_id, number))
      if (!blocker || blocker === row.key) continue
      const list = waiters.get(blocker) ?? []
      list.push(row.key)
      waiters.set(blocker, list)
    }
  }
  const counts = new Map<string, number>()
  for (const row of rows) {
    const seen = new Set<string>([row.key])
    const queue = [...(waiters.get(row.key) ?? [])]
    while (queue.length) {
      const key = queue.pop()!
      if (seen.has(key)) continue
      seen.add(key)
      queue.push(...(waiters.get(key) ?? []))
    }
    counts.set(row.key, seen.size - 1)
  }
  return counts
}

// Most unblocking first; ties keep the order they came in.
export function rankByUnblocks<T>(items: readonly T[], keyOf: (item: T) => string, counts: Map<string, number>): T[] {
  return items
    .map((item, index) => ({ item, index, n: counts.get(keyOf(item)) ?? 0 }))
    .sort((a, b) => b.n - a.n || a.index - b.index)
    .map(entry => entry.item)
}
