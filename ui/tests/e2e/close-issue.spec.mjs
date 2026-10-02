import assert from 'node:assert/strict'

// Closing an issue is the quiet way out of the pipeline: a reason is required,
// the dialog says what happens on GitHub, and the decision is submitted like
// every other one.
const SCREENSHOT = process.env.FORGE_E2E_ISSUE_SCREENSHOT || ''

async function hydrated(page) {
  await page.waitForFunction(() => document.querySelector('#__nuxt')?.__vue_app__?.config.globalProperties.$nuxt?.isHydrating === false)
}

export default async function (page, { base, scenario, requests }) {
  // Not closable (the default working issue): no Close action at all.
  await page.goto(base + '/issues/1:610')
  await hydrated(page)
  await page.locator('#issue-actions').waitFor()
  assert.equal(await page.getByRole('button', { name: 'Close issue…' }).count(), 0, 'hidden when the row is not closable')

  await scenario('close-issue')
  await page.setViewportSize({ width: 390, height: 900 })
  await page.goto(base + '/issues/1:620')
  await hydrated(page)
  await page.locator('#issue-workbench').waitFor()

  // The narrow page never scrolls sideways: the workbench stacks its cube
  // over the speech bubble, and the stage badges keep all five stages.
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'no horizontal scroll at 390px')
  assert.equal(await page.locator('#issue-workbench').getAttribute('aria-label'), 'Cube for #620, Security stage, needs attention')
  const bench = await page.locator('#issue-workbench').boundingBox()
  const bubble = await page.locator('#issue-bubble').boundingBox()
  assert.ok(bench.x >= 0 && bench.x + bench.width <= 390, 'the workbench fits the narrow width')
  assert.ok(bench.y + bench.height <= bubble.y, 'the bubble stacks under the cube')
  assert.equal(await page.locator('#issue-stages li').count(), 5)
  const stats = await page.locator('#issue-stats').boundingBox()
  assert.ok(stats.x >= 0 && stats.x + stats.width <= 390, 'the stats strip fits the narrow width')
  // One primary button for the way forward, directly under the bench; Close
  // is a quiet ghost button in the meta row under it.
  const actions = page.locator('#issue-actions')
  assert.equal(await actions.getByRole('button', { name: 'Retry attempt' }).count(), 1)
  const actionsBox = await actions.boundingBox()
  assert.ok(bubble.y + bubble.height <= actionsBox.y, 'the actions sit under the workbench')
  if (SCREENSHOT) await page.screenshot({ path: SCREENSHOT, fullPage: true })

  await page.locator('#issue-meta').getByRole('button', { name: 'Close issue…' }).click()
  const dialog = page.getByRole('dialog', { name: 'Close issue #620?' })
  await dialog.waitFor()
  const text = await dialog.innerText()
  assert.match(text, /Forge stops any running work/)
  assert.match(text, /Forge closes its pull request, PR #30\./)
  assert.match(text, /closes it on GitHub as “not planned”/)
  assert.match(text, /This does not delete the issue: GitHub only lets repository admins delete issues\. You can reopen it on GitHub later\./)

  // The reason is required.
  const confirm = dialog.getByRole('button', { name: 'Close issue', exact: true })
  assert.equal(await confirm.isDisabled(), true, 'no reason, no close')
  await dialog.getByRole('textbox', { name: 'Why are you closing it?' }).fill('   ')
  assert.equal(await confirm.isDisabled(), true, 'whitespace is not a reason')
  await dialog.getByRole('textbox', { name: 'Why are you closing it?' }).fill('Superseded by #640')
  assert.equal(await confirm.isDisabled(), false)
  await confirm.click()

  await page.getByRole('dialog').waitFor({ state: 'detached' })
  await page.waitForFunction(() => document.body.innerText.includes('Decision submitted. Waiting for confirmation.'))
  const posts = (await requests()).filter(entry => entry.method === 'POST' && entry.path === '/api/issues/action')
  assert.equal(posts.length, 1)
  assert.deepEqual(posts[0].body, { key: '1:620', action: 'close', revision: 'current', reason: 'Superseded by #640' })

  // Cancelling sends nothing.
  await scenario('close-issue')
  await page.goto(base + '/issues/1:620')
  await hydrated(page)
  await page.locator('#issue-meta').getByRole('button', { name: 'Close issue…' }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Cancel' }).click()
  await page.getByRole('dialog').waitFor({ state: 'detached' })
  assert.equal((await requests()).filter(entry => entry.method === 'POST').length, 0)
}
