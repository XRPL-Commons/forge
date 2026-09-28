import assert from 'node:assert/strict'

export default async function (page, { base, scenario, requests }) {
  await scenario('needs-decision')
  await page.goto(base + '/issues/1:610')
  await page.waitForFunction(() => window.useNuxtApp?.()?.isHydrating === false || document.querySelector('#issue-decision'))
  await page.getByRole('heading', { name: 'Wait for an upstream fix?' }).waitFor()
  assert.match(await page.locator('#issue-decision').innerText(), /Remove the dependency/)
  assert.match(await page.locator('#issue-decision').innerText(), /The supported release still contains the advisory/)
  for (const width of [320, 1440]) {
    await page.setViewportSize({ width, height: 1000 })
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
  }
  await page.getByRole('button', { name: 'Retry attempt', exact: true }).click()
  const dialog = page.getByRole('dialog')
  const confirm = dialog.getByRole('button', { name: 'Retry attempt', exact: true })
  assert.equal(await confirm.isDisabled(), true)
  await dialog.getByLabel('What changed?').fill('Remove the dependency and replace its usage')
  await confirm.click()
  await page.waitForFunction(() => !document.querySelector('[role="dialog"]'))
  const posts = (await requests()).filter(r => r.method === 'POST' && r.path === '/api/issues/action')
  assert.equal(posts.length, 1)
  assert.equal(posts[0].body.action, 'retry')
  assert.equal(posts[0].body.reason, 'Remove the dependency and replace its usage')
}
