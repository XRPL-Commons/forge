import { describe, it, expect } from 'vitest'
import { approvalLines, checkedText } from '../../app/utils/issues'
import { approverDraft, parseLogins, sameLogins } from '../../app/utils/approvers'

describe('approvalLines', () => {
  const time = () => '16:03'
  it('says whether each review counted, in words and with an icon', () => {
    const lines = approvalLines([
      { login: 'octo-operator', state: 'APPROVED', at: 1790000000, counted: true, why: null },
      { login: 'x', state: 'APPROVED', at: null, counted: false, why: 'No write access to the repository' },
      { login: 'y', state: 'CHANGES_REQUESTED', at: 1790000000, counted: false, why: 'Requested changes' },
      { login: 'z', state: 'DISMISSED', at: 1790000000, counted: false, why: 'Approval was dismissed' },
    ], time)
    expect(lines.map(line => line.text)).toEqual([
      '@octo-operator approved · 16:03 · counted',
      '@x approved · not counted: No write access to the repository',
      '@y requested changes · 16:03 · blocks the merge',
      '@z approval dismissed · 16:03 · not counted',
    ])
    expect(lines.map(line => line.icon)).toEqual(['i-lucide-circle-check', 'i-lucide-circle-x', 'i-lucide-circle-x', 'i-lucide-circle-x'])
  })
  it('omits the time before mount and tolerates older payloads', () => {
    expect(approvalLines([{ login: 'a', state: 'APPROVED', at: 1790000000, counted: true, why: null }])[0]!.text).toBe('@a approved · counted')
    expect(approvalLines(undefined)).toEqual([])
  })
})

describe('checkedText', () => {
  it('gives the age of the last GitHub read', () => {
    expect(checkedText(1000, 1020)).toBe('Checked on GitHub 20 sec ago')
    expect(checkedText(1000, 1000 + 180)).toBe('Checked on GitHub 3 min ago')
    expect(checkedText(1000, 0)).toBe('Checked on GitHub')
    expect(checkedText(null, 1000)).toBe('Not checked on GitHub yet')
    expect(checkedText(1010, 1000)).toBe('Checked on GitHub 0 sec ago')
  })
})

describe('approver list', () => {
  it('parses logins separated by commas, spaces or lines', () => {
    expect(parseLogins('@octocat, hubot\nOctocat  forge-bot')).toEqual({ logins: ['octocat', 'hubot', 'forge-bot'], invalid: [] })
    expect(parseLogins('ok, bad_name, a.b')).toEqual({ logins: ['ok'], invalid: ['bad_name', 'a.b'] })
  })
  it('saves an empty list for anyone, and refuses an empty or invalid allowlist', () => {
    expect(approverDraft('anyone', 'ignored')).toEqual({ logins: [], error: '' })
    expect(approverDraft('list', 'octocat')).toEqual({ logins: ['octocat'], error: '' })
    expect(approverDraft('list', '  ').error).toBe('Add at least one GitHub login.')
    expect(approverDraft('list', 'octo_cat').error).toBe('Not a GitHub login: octo_cat')
  })
  it('compares lists in order', () => {
    expect(sameLogins(['a', 'b'], ['a', 'b'])).toBe(true)
    expect(sameLogins(['a', 'b'], ['b', 'a'])).toBe(false)
    expect(sameLogins([], ['a'])).toBe(false)
  })
})
