import { describe, it, expect } from 'vitest'
import { pullLabel, pullName, sentence, technicalDetails } from '../../app/utils/issues'
import { spelled, spelledAgo } from '../../app/utils/format'

// The issue panel reads in full sentences, without abbreviations; the worker's
// technical report sits behind a toggle, never in the bubble.
describe('operator wording on the issue panel', () => {
  const report = 'GitHub rejected publication with GH013: refusing to allow a GitHub App to update `.github/workflows/quality.yml`.'
  const issue = { summary: { headline: "GitHub won't let Forge change workflow files in this repository", plain: 'Give the Forge app the Workflows permission.' },
    explanation: '', environment_blocker: report }

  it('keeps the technical report out of the bubble, behind its own toggle', () => {
    expect(technicalDetails(issue, [issue.summary.headline, issue.summary.plain])).toBe(report)
    // Nothing to reveal when the bubble already says it.
    expect(technicalDetails(issue, [issue.summary.headline, report])).toBe('')
    expect(technicalDetails({ summary: null, explanation: '', environment_blocker: null }, [])).toBe('')
  })

  it('turns terse notes into sentences', () => {
    expect(sentence('environment needs attention')).toBe('Environment needs attention.')
    expect(sentence('Done.')).toBe('Done.')
    expect(sentence('  ')).toBe('')
  })

  it('names pull requests in full on the panel and keeps the short card label', () => {
    expect(pullName({ number: 19, kind: 'candidate' } as never)).toBe('Pull request #19')
    expect(pullName({ number: 25, kind: 'external' } as never)).toBe('Other pull request #25')
    expect(pullLabel({ number: 19, kind: 'candidate' } as never)).toBe('PR #19')
  })

  it('spells durations out', () => {
    expect(spelled(1)).toBe('1 second')
    expect(spelled(720)).toBe('12 minutes')
    expect(spelled(3900)).toBe('1 hour 5 minutes')
    expect(spelledAgo(30)).toBe('just now')
    expect(spelledAgo(7300)).toBe('2 hours ago')
  })
})
