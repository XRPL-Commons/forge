import assert from 'node:assert/strict'

// An issue closed on GitHub as not planned or as a duplicate is `dropped`:
// "No longer needed", grey and resting, its own Show filter, no decisions
// on its page, and a "No longer needed" badge in Recently completed.

function waitForText(page, text) {
  return page.waitForFunction((needle) => document.body.innerText.includes(needle), text, { timeout: 10000 })
}

async function waitUntil(condition, what, timeout = 10000) {
  const deadline = Date.now() + timeout
  for (;;) {
    const value = await condition()
    if (value) return value
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${what}`)
    await new Promise((resolve) => setTimeout(resolve, 100))
  }
}

export default async function (page, { base, scenario, requests }) {
  const load = page.goto.bind(page)
  page.goto = async (...args) => {
    const response = await load(...args)
    await page.waitForFunction(() => {
      const app = document.querySelector('#__nuxt')?.__vue_app__
      return app?.config.globalProperties.$nuxt?.isHydrating === false
    }, undefined, { timeout: 15000 })
    return response
  }

  await scenario('dropped')
  await page.goto(base + '/issues')
  await waitForText(page, 'Port the quiz engine to the legacy runtime')

  // A grey chip with an archive icon beside the words, and a resting cube.
  const card = page.locator('[data-key="1:598"]')
  const chip = card.locator('[data-status-chip]')
  assert.equal((await chip.innerText()).trim(), 'No longer needed')
  assert.equal(await chip.getAttribute('data-tone'), 'calm')
  assert.ok(await chip.locator('.iconify, svg, span[class*="i-lucide"]').count() >= 1, 'the chip carries an icon, not colour alone')
  const look = await card.locator('canvas.cube-portrait').getAttribute('data-look')
  assert.match(look, /^queued rest/)
  assert.doesNotMatch(look, /beacon|bow|confetti/)

  // Its own Show filter, which asks the API for that view.
  await page.getByRole('button', { name: 'Show' }).click()
  await page.getByRole('option', { name: 'No longer needed' }).click()
  await waitUntil(async () => (await requests()).some((entry) => entry.path === '/api/issues' && entry.query.state === 'dropped'), 'a dropped view request')
  await page.waitForURL(base + '/issues?show=dropped')

  // Its page offers no decision, only the way back on GitHub.
  await scenario('dropped')
  await page.goto(base + '/issues/1%3A598')
  const actions = page.locator('#issue-actions')
  await actions.waitFor()
  assert.equal(await actions.locator('button').count(), 0, 'no actions on dropped work')
  assert.equal((await actions.innerText()).replace(/\s+/g, ' ').trim(), 'Nothing needed from you. Reopen it on GitHub to bring it back ↗')
  assert.equal(await page.locator('#issue-reopen').getAttribute('href'), 'https://github.com/XRPL-Commons/academy/issues/598')
  assert.equal(await page.locator('#issue-close').count(), 0)
  assert.equal((await page.locator('#issue-status').innerText()).trim(), 'No longer needed')
  assert.equal(await page.locator('#issue-workbench').getAttribute('data-mood'), 'rest')

  // Recently completed distinguishes dropped work from completed work.
  await scenario('dropped')
  await page.goto(base + '/')
  const CARDS = '#completed article.issue-card'
  await page.waitForFunction((selector) => document.querySelectorAll(selector).length > 0, CARDS, { timeout: 10000 })
  const dropped = page.locator(`${CARDS}[data-key="1:821"]`)
  assert.equal((await dropped.locator('[data-status-chip]').innerText()).trim(), 'No longer needed')
  assert.match(await dropped.locator('canvas').getAttribute('data-look'), /^queued rest/)
  // Merged issues stay Completed, whatever GitHub says about how they closed.
  assert.equal((await page.locator(`${CARDS}[data-key="1:820"] [data-status-chip]`).innerText()).trim(), 'Completed')
  assert.equal((await page.locator(`${CARDS}[data-key="1:825"] [data-status-chip]`).innerText()).trim(), 'Completed')
}
