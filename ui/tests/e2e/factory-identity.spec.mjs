import assert from 'node:assert/strict'

export default async function (page, { base, scenario, requests }) {
  await scenario('auth-identity')
  await page.goto(base + '/login')
  assert.equal(await page.title(), 'Sign in · Forge · Anvil')
  await page.getByText('Anvil', { exact: false }).first().waitFor()
  assert.equal(await page.locator('canvas.sign-in-scene').count(), 1)
  let reads = await requests()
  assert.deepEqual([...new Set(reads.filter(row => row.path.startsWith('/api/')).map(row => row.path))], ['/api/auth/session'])

  await page.context().addCookies([{ name: 'synthetic_operator', value: 'yes', url: base }])
  await page.goto(base + '/settings/repositories')
  await page.getByRole('link', { name: 'Forge · Anvil home' }).waitFor()
  assert.equal(await page.title(), 'Repositories · Forge · Anvil')
  await page.screenshot({ path: '/tmp/forge-primary-brand.png', animations: 'disabled' })
  await page.getByRole('link', { name: 'Settings', exact: true }).first().click()
  await page.waitForURL(base + '/settings')
  await page.waitForFunction(() => document.title === 'Settings · Forge · Anvil')
  assert.equal(await page.title(), 'Settings · Forge · Anvil')
  reads = await requests()
  assert.equal(reads.filter(row => row.path === '/api/auth/session').length, 2, 'identity reuses session cache during navigation')

  await scenario('auth-identity-long')
  for (const width of [320, 390, 1280]) {
    await page.setViewportSize({ width, height: 844 })
    await page.goto(base + '/settings/repositories')
    await page.getByRole('link', { name: /verylongfactoryname.* home/ }).waitFor()
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `name wraps at ${width}px`)
  }
  await page.context().clearCookies()
  for (const width of [320, 390, 1280]) {
    await page.setViewportSize({ width, height: 844 })
    await page.goto(base + '/login')
    await page.getByRole('link', { name: 'Continue with GitHub' }).waitFor()
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `login name wraps at ${width}px`)
  }
  await scenario('auth-required')
  await page.goto(base + '/login')
  assert.equal(await page.title(), 'Sign in · Forge', 'old installations retain the product fallback')
}
