import assert from 'node:assert/strict'

// A decision takes a second or more to come back. While it is in flight the
// confirm button spins under the action's own verb, refuses a second click,
// and the dialog stays open until Forge answers — then it closes on success.

async function hydrated(page) {
  await page.waitForFunction(() => document.querySelector('#__nuxt')?.__vue_app__?.config.globalProperties.$nuxt?.isHydrating === false)
}

const DELAY_MS = 1500

export default async function (page, { base, scenario, requests }) {
  await scenario('close-issue')
  let release
  let intercepted = 0
  const gate = new Promise((resolve) => { release = resolve })
  await page.route('**/api/issues/action', async (route) => {
    intercepted += 1
    await Promise.race([gate, new Promise(resolve => setTimeout(resolve, DELAY_MS * 4))])
    await route.continue()
  })

  try {
    await page.goto(base + '/issues/1:620')
    await hydrated(page)
    await page.locator('#issue-actions').getByRole('button', { name: 'Retry attempt' }).click()
    const dialog = page.getByRole('dialog', { name: 'Retry attempt?' })
    await dialog.waitFor()
    await dialog.getByRole('textbox', { name: 'What changed?' }).fill('Fixed the flaky fixture')

    const confirm = dialog.locator('[data-confirm]')
    assert.equal(await confirm.innerText(), 'Retry attempt')
    // Two quick clicks: the second lands while the first is in flight.
    await confirm.click()
    await confirm.click({ force: true }).catch(() => {})

    await page.waitForFunction(() => document.querySelector('[role=dialog] [data-confirm]')?.textContent?.includes('Retrying…'))
    assert.equal(await confirm.isDisabled(), true, 'disabled while in flight')
    assert.equal(await confirm.getAttribute('aria-busy'), 'true')
    assert.ok(await confirm.locator('.animate-spin, [class*="animate-spin"]').count() > 0, 'the button shows a spinner')
    assert.equal(await dialog.getByRole('button', { name: 'Close' }).isDisabled(), true, 'the dialog cannot be dismissed mid-request')
    await page.keyboard.press('Escape')
    assert.equal(await dialog.isVisible(), true, 'the dialog stays open until Forge answers')

    release()
    await page.getByRole('dialog').waitFor({ state: 'detached' })
    const posts = (await requests()).filter(entry => entry.method === 'POST' && entry.path === '/api/issues/action')
    assert.equal(intercepted, 1, 'the browser sent one request')
    assert.equal(posts.length, 1, 'Forge received one decision')
    assert.equal(posts[0].body.action, 'retry')
  } finally {
    release()
    await page.unroute('**/api/issues/action')
  }
}
