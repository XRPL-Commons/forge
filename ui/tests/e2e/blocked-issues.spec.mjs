import assert from 'node:assert/strict'

// An issue waiting on other issues has its own status, `blocked`: a calm chip
// naming what it waits on, a patient cube tied to its prerequisites, its own
// Show filter, and an action area that asks nothing of the operator.

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

  await scenario('blocked')
  await page.goto(base + '/issues')
  await waitForText(page, 'Show course progress on the dashboard')

  // The chip names the open prerequisites, calmly, with a link icon beside
  // the words; the cube waits patiently and is never lit amber.
  const card = page.locator('[data-key="1:612"]')
  const chip = card.locator('[data-status-chip]')
  assert.equal((await chip.innerText()).trim(), 'Waiting on #590, #601')
  assert.equal(await chip.getAttribute('data-tone'), 'calm')
  assert.ok(await chip.locator('.iconify, svg, span[class*="i-lucide"]').count() >= 1, 'the chip carries an icon, not colour alone')
  const look = await card.locator('canvas.cube-portrait').getAttribute('data-look')
  assert.match(look, /patient/)
  assert.doesNotMatch(look, /beacon/)
  assert.doesNotMatch(await card.innerText(), /Needs attention/)
  // The attention row next to it still reads as needing you.
  assert.equal((await page.locator('[data-key="1:613"] [data-status-chip]').innerText()).trim(), 'Needs attention')

  // Its own Show filter, which asks the API for that view.
  await page.getByRole('button', { name: 'Show' }).click()
  await page.getByRole('option', { name: 'Waiting on other issues' }).click()
  await waitUntil(async () => (await requests()).some((entry) => entry.path === '/api/issues' && entry.query.state === 'blocked'), 'a blocked view request')
  await page.waitForURL(base + '/issues?show=blocked')

  // The issue page: nothing needed, and each prerequisite opens its issue.
  await scenario('blocked')
  await page.goto(base + '/issues/1%3A612')
  const nothing = page.locator('#issue-nothing-needed')
  await nothing.waitFor()
  assert.equal((await nothing.innerText()).replace(/\s+/g, ' ').trim(), 'Nothing needed from you. Starts by itself when #590 and #601 close.')
  assert.equal(await nothing.locator('[data-blocking="590"]').getAttribute('href'), '/issues/1%3A590')
  assert.equal(await nothing.locator('[data-blocking="601"]').getAttribute('href'), '/issues/1%3A601')
  assert.equal((await page.locator('#issue-status').innerText()).trim(), 'Waiting on #590, #601')
  assert.equal(await page.locator('#issue-workbench').getAttribute('data-mood'), 'patient')
  assert.match(await page.locator('#issue-workbench').getAttribute('aria-label'), /waiting on other issues$/)
  assert.equal(await page.locator('#issue-bubble.attention').count(), 0)

  // In the board's panel, a prerequisite opens in the panel itself.
  await page.goto(base + '/?issue=1:612')
  const panel = page.locator('[role="dialog"][data-side="right"]')
  await panel.locator('#issue-nothing-needed [data-blocking="590"]').waitFor()
  assert.equal(await panel.locator('[data-blocking="590"]').getAttribute('href'), '/?issue=1:590')
}
