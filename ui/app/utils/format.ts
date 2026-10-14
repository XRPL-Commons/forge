export function formatAge(seconds: number): string {
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return Math.floor(seconds / 60) + ' min'
  if (seconds < 86400) return Math.floor(seconds / 3600) + ' h'
  return Math.floor(seconds / 86400) + ' d'
}
// The age of something that already happened. `formatAge` says "just now"
// rather than "0 min", so the suffix is only added where it reads.
export function ago(seconds: number): string {
  const age = formatAge(Math.max(0, Math.floor(seconds)))
  return age === 'just now' ? age : age + ' ago'
}
// A commit as an operator reads it: the seven characters GitHub shows.
export function shortCommit(commit: string | null | undefined): string {
  return (commit ?? '').slice(0, 7)
}
// A duration in words, for a panel read in sentences.
export function spelled(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds))
  const unit = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`
  if (total < 60) return unit(total, 'second')
  if (total < 3600) return unit(Math.floor(total / 60), 'minute')
  const minutes = Math.floor((total % 3600) / 60)
  return unit(Math.floor(total / 3600), 'hour') + (minutes ? ` ${unit(minutes, 'minute')}` : '')
}

// "12 minutes ago": an age in words, for sentences.
export function spelledAgo(seconds: number): string {
  return seconds < 60 ? 'just now' : `${spelled(seconds < 3600 ? Math.floor(seconds / 60) * 60 : Math.floor(seconds / 3600) * 3600)} ago`
}
// Requested reviewers are always shown as GitHub mentions, so the same people
// read the same way on an Activity card and on the issue itself.
export function reviewerMentions(logins: string[] | null | undefined): string {
  return (logins ?? []).filter(Boolean).map(login => '@' + login.replace(/^@+/, '')).join(', ')
}
export function clampTitle(text: string, max = 96): string {
  if (text.length <= max) return text
  const cut = text.slice(0, max); const space = cut.lastIndexOf(' ')
  return (space > max * 0.6 ? cut.slice(0, space) : cut).trimEnd() + '…'
}
