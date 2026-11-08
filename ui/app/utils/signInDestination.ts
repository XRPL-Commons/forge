// Keep in sync with operator_auth.sign_in_destination. Only these local routes
// may be restored after sign-in; arbitrary URLs and encoded paths are refused.
export function signInDestination(value: unknown, fallback = '/'): string {
  return typeof value === 'string' && value.trim() === value && /^(?:\/|\/installation|\/settings\/agents(?:\?code=[A-Z2-9]{4}-[A-Z2-9]{4})?|\/onboarding\/cli\/github\/start\?state=[A-Za-z0-9_-]{43})$/.test(value)
    ? value : fallback
}
