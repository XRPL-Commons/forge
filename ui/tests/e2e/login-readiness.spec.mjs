import assert from 'node:assert/strict'

export default async function (page, { base, scenario }) {
  async function openPending() {
    await scenario('default')
    await page.context().clearCookies()
    await page.goto(base + '/')
    await page.waitForFunction(() => document.querySelector('#__nuxt')?.__vue_app__?.config.globalProperties.$nuxt?.isHydrating === false)
    // Reproduce an already-open browser's startup config, before App consent.
    // The production server may already have restarted with setupPending=false.
    await page.evaluate(async () => {
      const globals = document.querySelector('#__nuxt').__vue_app__.config.globalProperties
      globals.$nuxt.$config.public.forgeSetupPending = true
      await globals.$router.push('/login?return_to=%2Finstallation')
    })
    await page.getByRole('heading', { name: 'Your factory is getting ready' }).waitFor()
  }

  await openPending()
  for (const width of [320, 390]) {
    await page.setViewportSize({ width, height: 844 })
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `setup fits ${width}px`)
  }
  await scenario('auth-required')
  const signIn = page.getByRole('link', { name: 'Continue with GitHub' })
  await signIn.waitFor({ timeout: 10000 })
  assert.equal(await signIn.getAttribute('href'), '/auth/github/start?return_to=%2Finstallation')
  assert.equal(await page.getByRole('heading', { name: 'Your factory is getting ready' }).count(), 0)
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))

  await openPending()
  await page.context().addCookies([{ name: 'synthetic_operator', value: 'yes', url: base }])
  await scenario('auth-identity')
  await page.waitForURL(base + '/installation', { timeout: 10000 })
  assert.equal(await page.getByRole('heading', { name: 'Your factory is getting ready' }).count(), 0)
}
