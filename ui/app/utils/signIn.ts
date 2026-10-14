// The sign-in page: which friendly notice to show for what the auth flow sent.
type SignInTone = 'coral' | 'amber' | 'mint'

interface SignInNotice {
  kind: 'unavailable' | 'failed' | 'return'
  tone: SignInTone
  // Errors are announced; the return hint is a polite status.
  role: 'alert' | 'status'
  title: string
  text: string
}

interface SignInInput {
  // `/auth/github/callback` sends `?error=sign_in` for every failed attempt:
  // not authorized, expired, wrong browser, or GitHub refused. It names no
  // account and gives no reason, so neither does the page.
  error?: unknown
  // The operator middleware adds `return_to` only for /settings/agents.
  returnTo?: unknown
  // `/api/auth/session` did not answer.
  unavailable: boolean
}

export function signInNotice({ error, returnTo, unavailable }: SignInInput): SignInNotice | null {
  if (unavailable) {
    return {
      kind: 'unavailable', tone: 'amber', role: 'alert',
      title: 'The sign-in desk is napping',
      text: 'Forge couldn’t reach its sign-in service. Give it a moment, then try again.',
    }
  }
  if (error) {
    return {
      kind: 'failed', tone: 'coral', role: 'alert',
      title: 'That sign-in didn’t go through',
      text: 'This GitHub account may not be on the list for this Forge, or the sign-in took too long and expired. Try again, or ask your Forge administrator to add your account.',
    }
  }
  if (typeof returnTo === 'string' && returnTo.startsWith('/settings/agents')) {
    return {
      kind: 'return', tone: 'mint', role: 'status',
      title: 'Almost there',
      text: 'Sign in to finish connecting your agent. You’ll come straight back here.',
    }
  }
  return null
}
