import { describe, it, expect } from 'vitest'
import { rankByUnblocks, unblockCounts, type DependencyRow } from '../../app/utils/unblocks'

function row(number: number, on: number[] = [], repo = 1): DependencyRow {
  return { key: `${repo}:${number}`, repo_id: repo, number, blocked_dependencies: on }
}
const count = (rows: DependencyRow[]) => Object.fromEntries(unblockCounts(rows))

describe('unblockCounts', () => {
  it('counts every issue that waits on another through a chain', () => {
    // 3 waits on 2, 2 waits on 1: closing 1 unblocks 2 and then 3.
    expect(count([row(1), row(2, [1]), row(3, [2])])).toEqual({ '1:1': 2, '1:2': 1, '1:3': 0 })
  })

  it('counts a diamond once per issue', () => {
    // 2 and 3 wait on 1; 4 waits on both 2 and 3.
    expect(count([row(1), row(2, [1]), row(3, [1]), row(4, [2, 3])])).toEqual({ '1:1': 3, '1:2': 1, '1:3': 1, '1:4': 0 })
  })

  it('guards against cycles and self-dependencies', () => {
    expect(count([row(1, [3]), row(2, [1]), row(3, [2])])).toEqual({ '1:1': 2, '1:2': 2, '1:3': 2 })
    expect(count([row(5, [5])])).toEqual({ '1:5': 0 })
    // A cycle hanging off a chain still terminates and counts each issue once.
    expect(count([row(1), row(2, [1, 3]), row(3, [2])])).toEqual({ '1:1': 2, '1:2': 1, '1:3': 1 })
  })

  it('keeps repositories apart and ignores closed dependencies', () => {
    // Repo 2's #10 waits on repo 2's #1, not on repo 1's; #99 is not open.
    const counts = count([row(1), row(1, [], 2), row(10, [1], 2), row(11, [99])])
    expect(counts).toEqual({ '1:1': 0, '2:1': 1, '2:10': 0, '1:11': 0 })
  })

  it('tolerates rows without dependencies', () => {
    expect(count([{ key: '1:1', repo_id: 1, number: 1 }, { key: '1:2', repo_id: 1, number: 2, blocked_dependencies: null }])).toEqual({ '1:1': 0, '1:2': 0 })
  })

  it('ranks by unblocks, keeping the incoming order for ties', () => {
    const counts = new Map([['a', 1], ['b', 7], ['c', 0], ['d', 7], ['e', 12]])
    expect(rankByUnblocks(['a', 'b', 'c', 'd', 'e', 'f'], x => x, counts)).toEqual(['e', 'b', 'd', 'a', 'c', 'f'])
  })
})
