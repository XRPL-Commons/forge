// The optional approver allowlist (runtime `human_reviewers`). Empty means
// anyone with write access to the repository can approve.

const LOGIN = /^[A-Za-z0-9-]+$/

// Logins typed one per line or separated by commas or spaces; a leading "@"
// is accepted and dropped, duplicates (GitHub logins ignore case) collapse.
export function parseLogins(text: string): { logins: string[]; invalid: string[] } {
  const logins: string[] = []
  const invalid: string[] = []
  const seen = new Set<string>()
  for (const raw of text.split(/[\s,]+/)) {
    const login = raw.replace(/^@+/, '')
    if (!login) continue
    if (!LOGIN.test(login)) { invalid.push(raw); continue }
    if (seen.has(login.toLowerCase())) continue
    seen.add(login.toLowerCase())
    logins.push(login)
  }
  return { logins, invalid }
}

// What the form would save, or why it cannot.
export function approverDraft(mode: 'anyone' | 'list', text: string): { logins: string[]; error: string } {
  if (mode === 'anyone') return { logins: [], error: '' }
  const { logins, invalid } = parseLogins(text)
  if (invalid.length) return { logins, error: `Not a GitHub login: ${invalid.join(', ')}` }
  if (!logins.length) return { logins, error: 'Add at least one GitHub login.' }
  return { logins, error: '' }
}

export function sameLogins(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((login, index) => login === b[index])
}
