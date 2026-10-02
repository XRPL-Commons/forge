import assert from 'node:assert/strict'

export default async function (page, { base, scenario }) {
  await scenario('owned')
  await page.goto(base + '/')
  const section = page.locator('#owned')
  await section.waitFor()
  assert.match(await section.innerText(), /Assigned to @lucbocahut/)
  assert.equal(await page.locator('#waiting').count(), 0)
  assert.equal(await page.locator('#columns [data-key="1:610"]').count(), 0)
  for (const width of [390, 1440]) {
    await page.setViewportSize({ width, height: 1000 })
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    await page.screenshot({ path: `/tmp/forge-135-owned-${width}.png`, fullPage: true, animations: 'disabled' })
  }
  await section.locator('[data-key="1:610"]').click()
  const panel = page.locator('[role="dialog"][data-side="right"]')
  await panel.waitFor()
  await panel.getByText('Assigned to @lucbocahut', { exact: true }).waitFor()
  assert.match(await panel.innerText(), /Unassign it on GitHub/)
  await page.keyboard.press('Escape')
  await panel.waitFor({ state: 'hidden' })

  // The status line's issue link opens the same panel and stays on the board.
  const statusLink = page.getByRole('link', { name: 'View issue' })
  assert.equal(await statusLink.getAttribute('href'), '/?issue=1:610')
  await statusLink.click()
  await panel.waitFor()
  assert.equal(new URL(page.url()).pathname, '/')
}
