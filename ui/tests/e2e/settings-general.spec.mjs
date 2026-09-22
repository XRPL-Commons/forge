import assert from 'node:assert/strict'

const PROVIDER_ERROR = 'The claude native tool sandbox failed verification inside the runner. Check the runner image and the host isolation policies'
const LOCKED_REASON = 'Use guided setup until the factory has started'

function occurrences(page, text) {
  return page.evaluate((needle) => document.body.innerText.split(needle).length - 1, text)
}

function waitForText(page, text) {
  return page.waitForFunction((needle) => document.body.innerText.includes(needle), text, { timeout: 10000 })
}

// End-to-end behaviour of Settings · General as the factory status page: the
// five status rows, the single-producer warning banner (and its interplay
// with the accounts row's own sentence), the worker-capacity save/discard
// round-trip against the full FactoryView echo, the guided-setup lock, the
// paused-factory fence, and a stacked mobile layout.
export default async function (page, { base, scenario, requests }) {
  await page.goto(base + '/settings')
  await waitForText(page, 'Manage worker capacity and factory setup.')

  // Five status rows, each reporting the default fixture's state.
  const rows = page.locator('[data-status-row]')
  assert.equal(await rows.count(), 5, 'expected the five status rows')
  await waitForText(page, 'Enabled')
  await waitForText(page, 'forge/worker:2026.09 · verification current')
  await waitForText(page, 'All coding accounts verified.')
  await waitForText(page, '4 of 4 Apps verified')
  await waitForText(page, 'Supervisor seen')

  // The onboarding error is a single sentence, rendered once by the banner
  // (with its own remediation button) — never repeated inside the accounts
  // row, which only ever carries the raw per-harness status words.
  await scenario('provider-failed')
  await waitForText(page, PROVIDER_ERROR)
  assert.equal(await occurrences(page, PROVIDER_ERROR), 1, 'the failure sentence must render exactly once')
  const openAccounts = page.getByRole('link', { name: 'Open coding accounts' })
  assert.equal(await openAccounts.count(), 1)
  assert.equal(await openAccounts.getAttribute('href'), '/settings/harnesses')
  const accountsRow = await page.locator('[data-status-row="accounts"]').innerText()
  assert.ok(!accountsRow.includes(PROVIDER_ERROR), 'the accounts row must not repeat the banner sentence')
  assert.ok(accountsRow.includes('Claude Code failed'), `expected the raw status in the accounts row, saw: ${accountsRow}`)

  // Back to a clean baseline for the capacity form.
  await scenario('default')
  await page.reload()
  await waitForText(page, 'All coding accounts verified.')

  const builders = page.locator('#capacity-build')
  await builders.waitFor({ state: 'visible' })
  assert.equal(await builders.inputValue(), '2')

  // Discard restores the original value without posting anything.
  await builders.fill('5')
  await page.getByRole('button', { name: 'Discard changes' }).click()
  await waitForText(page, 'Changes discarded.')
  assert.equal(await builders.inputValue(), '2')
  assert.equal((await requests()).filter((entry) => entry.method === 'POST' && entry.path === '/api/runtime').length, 0)

  // Save posts the full runtime with only slots.build changed, then the
  // inputs re-render from the POST response's FactoryView (runtime.post.json
  // echoes slots.build: 3 back), not just the value that was typed.
  await builders.fill('3')
  await page.getByRole('button', { name: 'Save worker limits' }).click()
  await waitForText(page, 'Worker limits saved. They apply as workers become available.')

  const posted = (await requests()).filter((entry) => entry.method === 'POST' && entry.path === '/api/runtime')
  assert.equal(posted.length, 1)
  assert.equal(posted[0].body.revision, '6f2a9c8e1d4b7035af6c2e9d10b4f7a3c5e8d1b6924a7f3c0e5b8a1d4f7c2e9b')
  assert.equal(posted[0].body.config.slots.build, 3)
  assert.equal(posted[0].body.config.slots.review, 1)
  assert.equal(posted[0].body.config.image, 'forge/worker:2026.09')
  assert.equal(await builders.inputValue(), '3')

  // A setup phase short of 'active' locks the inputs and names the guard.
  await scenario('guided-setup')
  await page.reload()
  await waitForText(page, LOCKED_REASON)
  assert.equal(await page.locator('#capacity-build').isDisabled(), true)
  assert.equal(await page.getByRole('button', { name: 'Save worker limits' }).count(), 0)

  // A reset fence (both /api/settings and /api/factory) surfaces its reason
  // through the freshness notice, once.
  await scenario('default')
  await page.reload()
  await waitForText(page, 'All coding accounts verified.')
  await scenario('paused')
  await waitForText(page, 'Factory reset is in progress')
  assert.equal(await occurrences(page, 'Factory reset is in progress'), 1)

  // Mobile: each status row stacks its label/value above its link instead of
  // laying them out side by side.
  await scenario('default')
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(base + '/settings')
  await waitForText(page, 'All coding accounts verified.')
  const direction = await page.locator('[data-status-row="dispatch"]').evaluate((el) => getComputedStyle(el).flexDirection)
  assert.equal(direction, 'column', 'status rows must stack on a phone-width viewport')
  const overflow = await page.evaluate(() => ({ scrollWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth }))
  assert.ok(overflow.scrollWidth <= overflow.innerWidth, `page scrolls sideways: ${JSON.stringify(overflow)}`)
}
