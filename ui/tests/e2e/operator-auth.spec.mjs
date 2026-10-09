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

  // The friendly page: a heading, the one-line explanation, the decorative
  // scene hidden from assistive technology, Ember waving hello.
  await page.getByRole('heading', { level: 1, name: 'Welcome back to the factory floor' }).waitFor()
  await page.getByText('Sign in with the GitHub account your Forge administrator authorized.').waitFor()
  const scene = page.locator('canvas.sign-in-scene')
  assert.equal(await scene.getAttribute('aria-hidden'), 'true')
  assert.equal(await scene.getAttribute('data-mood'), 'waving')
  assert.equal(await page.getByRole('alert').count(), 0)

  // Every failed callback lands on ?error=sign_in: a worried cube, no account named.
  await page.goto(base + '/login?error=sign_in')
  await page.getByRole('alert').filter({ hasText: 'That sign-in didn’t go through' }).waitFor()
  await page.getByRole('alert').filter({ hasText: 'ask your Forge administrator to add your account' }).waitFor()
  assert.equal(await page.locator('canvas.sign-in-scene').getAttribute('data-mood'), 'worried')
  assert.equal(await page.getByRole('link', { name: 'Continue with GitHub' }).getAttribute('href'), '/auth/github/start')

  // Coming back from an agent connection keeps return_to on the same start URL.
  await page.goto(base + '/login?return_to=' + encodeURIComponent('/settings/agents?code=ABCD-EFGH'))
  await page.getByRole('status').filter({ hasText: 'Sign in to finish connecting your agent' }).waitFor()
  assert.equal(await page.locator('canvas.sign-in-scene').getAttribute('data-mood'), 'hopeful')
  assert.equal(await page.getByRole('link', { name: 'Continue with GitHub' }).getAttribute('href'),
    '/auth/github/start?return_to=' + encodeURIComponent('/settings/agents?code=ABCD-EFGH'))

  // Before sign-in the page reads nothing but the session, even while animating.
  await page.goto(base + '/login')
  await page.locator('canvas.sign-in-scene[data-motion="moving"]').waitFor()
  await page.waitForTimeout(600)
  reads = await requests()
  assert.deepEqual([...new Set(reads.filter(r => r.path.startsWith('/api/')).map(r => r.path))], ['/api/auth/session'],
    'the sign-in page must not call private APIs')

  // The scene animates, but reduced motion gives one still frame.
  const shot = () => page.locator('canvas.sign-in-scene').evaluate(c => c.toDataURL())
  const a = await shot(); await page.waitForTimeout(350)
  assert.notEqual(await shot(), a, 'the scene moves by default')
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.reload()
  await page.locator('canvas.sign-in-scene[data-motion="still"]').waitFor()
  await page.waitForTimeout(200)
  const still = await shot(); await page.waitForTimeout(350)
  assert.equal(await shot(), still, 'reduced motion draws one static frame')
  await page.emulateMedia({ reducedMotion: null })

  // A phone: the scene above the card, nothing wider than the screen.
  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByRole('link', { name: 'Continue with GitHub' }).waitFor()
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'no horizontal scroll at 390px')
  const sceneBox = await page.locator('canvas.sign-in-scene').boundingBox()
  const cardBox = await page.locator('section[aria-labelledby="sign-in-title"]').boundingBox()
  assert.ok(sceneBox && cardBox && sceneBox.y < cardBox.y, 'the scene sits above the card')
  await page.setViewportSize({ width: 1280, height: 800 })

  // While the browser leaves for GitHub the button says so. (The test keeps
  // the browser here by cancelling the navigation after the page handled the click.)
  await page.evaluate(() => window.addEventListener('click', e => e.preventDefault(), { once: true }))
  await page.getByRole('link', { name: 'Continue with GitHub' }).click()
  await page.getByRole('link', { name: 'Opening GitHub…' }).waitFor()
  assert.equal(await page.getByRole('link', { name: 'Opening GitHub…' }).getAttribute('aria-busy'), 'true')

  // A real click requests exactly the same OAuth start URL as before.
  await page.reload()
  let started = ''
  await page.route('**/auth/github/start**', route => { started = route.request().url(); return route.fulfill({ status: 200, contentType: 'text/html', body: 'GitHub' }) })
  await page.getByRole('link', { name: 'Continue with GitHub' }).click()
  await page.waitForURL(base + '/auth/github/start')
  assert.equal(started, base + '/auth/github/start')
  await page.unroute('**/auth/github/start**')

  // The sign-in service is down: a sleepy cube and a way to retry.
  await scenario('down')
  await page.goto(base + '/login')
  await page.getByRole('alert').filter({ hasText: 'The sign-in desk is napping' }).waitFor()
  assert.equal(await page.locator('canvas.sign-in-scene').getAttribute('data-mood'), 'sleepy')
  await page.getByRole('button', { name: 'Try again' }).waitFor()
  assert.equal(await page.getByRole('link', { name: 'Continue with GitHub' }).count(), 0)
  await scenario('auth-required')
  await page.getByRole('button', { name: 'Try again' }).click()
  await page.getByRole('link', { name: 'Continue with GitHub' }).waitFor()
  assert.equal(await page.locator('canvas.sign-in-scene').getAttribute('data-mood'), 'waving')

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
