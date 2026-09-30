import assert from 'node:assert/strict'

export default async function (page, { base, scenario, requests }) {
  await scenario('auth-required')
  const response = await page.goto(base + '/settings')
  await page.waitForURL(base + '/login')
  assert.equal(await page.title(), 'Sign in · Forge')
  assert.equal(await page.getByRole('link', { name: 'Continue with GitHub' }).getAttribute('href'), '/auth/github/start')
  assert.ok(!(await response.text()).includes('controls_token'))
  let reads = await requests()
  assert.equal(reads.filter(r => r.path.startsWith('/api/') && r.path !== '/api/auth/session').length, 0,
    'signed-out SSR must not read private API data')
  assert.equal(await page.getByRole('navigation').count(), 0)

  await page.goto(base + '/login?error=sign_in')
  await page.getByRole('alert').filter({ hasText: 'Sign-in did not complete' }).waitFor()
  await page.context().addCookies([{ name: 'synthetic_operator', value: 'yes', url: base }])
  await page.goto(base + '/')
  const account = page.getByRole('button', { name: 'Account menu for forge-operator' })
  await account.waitFor()
  assert.equal(await page.getByText('forge-operator', { exact: true }).count(), 0)
  await account.click()
  await page.getByRole('menu').getByText('forge-operator', { exact: true }).waitFor()
  await page.getByRole('menuitem', { name: 'Sign out' }).click()
  await page.waitForURL(base + '/login')
  assert.equal(await page.getByText('forge-operator', { exact: true }).count(), 0)
  reads = await requests()
  assert.equal(reads.filter(r => r.path === '/api/auth/logout' && r.method === 'POST').length, 1)

  // Expiry during an active page clears cached private responses and signs out.
  await page.context().addCookies([{ name: 'synthetic_operator', value: 'yes', url: base }])
  await page.goto(base + '/')
  await page.getByRole('button', { name: 'Account menu for forge-operator' }).waitFor()
  await page.context().clearCookies()
  await scenario('auth-expired')
  // Concurrent expired reads can replace an in-flight login navigation.
  await page.getByRole('link', { name: 'Continue with GitHub' }).waitFor({ timeout: 15000 })
  assert.equal(page.url(), base + '/login')
}
