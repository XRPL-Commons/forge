import assert from 'node:assert/strict'
export default async function (page, { base, requests }) {
  await page.addInitScript(() => { Object.defineProperty(crypto, 'randomUUID', { value: undefined, configurable: true }) })
  await page.goto(base + '/issues/1:610')
  await page.getByRole('button', { name: 'Discuss blocker', exact: true }).click()
  await page.getByRole('textbox', { name: 'Ask about this issue' }).fill('Why did the last attempt fail?')
  await page.getByRole('button', { name: 'Send', exact: true }).click()
  await page.getByRole('button', { name: 'Apply this decision' }).waitFor()
  assert.match(await page.locator('body').innerText(), /new conversation using saved issue evidence/i)
  assert.match(await page.locator('body').innerText(), /shared type is incorrect/)
  assert.equal((await requests()).filter(r => r.method === 'POST' && r.path === '/api/issues/action').length, 0)
  await page.getByRole('button', { name: 'Apply this decision' }).click()
  await page.getByText('Decision submitted. The issue status above shows whether Forge accepted it.').waitFor()
  const actions = (await requests()).filter(r => r.method === 'POST' && r.path === '/api/issues/discussion')
  assert.deepEqual(actions.map(r => r.body.action), ['send', 'apply'])
  await page.reload()
  await page.getByRole('button', { name: 'Discuss blocker', exact: true }).click()
  await page.getByText('Decision submitted. The issue status above shows whether Forge accepted it.').waitFor()
  for (const width of [320, 390, 1440]) {
    await page.setViewportSize({ width, height: 1000 })
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `no overflow at ${width}`)
    await page.screenshot({ path: `/tmp/forge-conversation-${width}.png`, fullPage: true })
  }
}
