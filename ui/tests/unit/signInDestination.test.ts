import { expect, it } from 'vitest'
import { signInDestination } from '../../app/utils/signInDestination'

it('restores only the existing sign-in destinations and installation', () => {
  for (const path of ['/', '/installation', '/settings/agents', '/settings/agents?code=ABCD-EFGH', '/onboarding/cli/github/start?state=' + 'a'.repeat(43)]) {
    expect(signInDestination(path)).toBe(path)
  }
  for (const path of [undefined, ['/installation'], '//external.example', 'https://external.example', '/\\external.example', '/%2fexternal.example', '/installation?return_to=https://external.example', '/installation/../settings', '/login', '/settings/agents-extra', '/settings/agents\n']) {
    expect(signInDestination(path, '/installation')).toBe('/installation')
  }
})
