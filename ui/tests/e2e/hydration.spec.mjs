import assert from 'node:assert/strict'

export default async function(page, { base, scenario }) {
  // Exercise a browser locale/timezone different from the server's defaults.
  const context = await page.context().browser().newContext({ locale: 'fr-FR', timezoneId: 'Pacific/Honolulu' })
  try {
    for (const path of ['/settings/github', '/settings/repositories/2']) {
      const view = await context.newPage()
      const errors = []
      view.on('console', message => { if (/hydration/i.test(message.text())) errors.push(message.text()) })
      view.on('pageerror', error => errors.push(error.message))
      await view.goto(base + path)
      await view.waitForFunction(() => document.querySelector('#__nuxt')?.__vue_app__?.config.globalProperties.$nuxt?.isHydrating === false)
      assert.deepEqual(errors, [], `${path} hydrates without timezone or locale mismatches`)
      const text = await view.locator(path.includes('github') ? '#sync-status' : '#repo-checked-at').textContent()
      assert.ok(text.startsWith(path.includes('github') ? 'Last refresh ' : 'Last checked '))
      assert.ok(!/\d{4}-\d{2}-\d{2}T/.test(text), 'mounted dates use the browser locale')
      await view.close()
    }
  } finally { await context.close() }

  // Server-rendered controls must not accept edits or submit native forms
  // before Vue installs their handlers (also covers unavailable JavaScript).
  const pending = await page.context().browser().newContext({ javaScriptEnabled: false })
  try {
    const view = await pending.newPage()
    await view.goto(base + '/settings')
    assert.equal(await view.locator('#approvers-logins').isDisabled(), true)
    await scenario('auth-agents')
    await pending.addCookies([{ name: 'synthetic_operator', value: 'yes', url: base }])
    await view.goto(base + '/settings/agents?code=BCDF-2345')
    assert.equal(await view.getByLabel('Connection code').isDisabled(), true)
    assert.equal(await view.getByRole('button', { name: 'Review request' }).isDisabled(), true)
  } finally { await pending.close() }
}
