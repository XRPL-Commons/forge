import assert from 'node:assert/strict'

// An issue someone else owns sleeps on the factory line's shelf: it is not
// a card on Activity, and it is not in the list of issues that need you.
export default async function (page, { base, scenario }) {
  await scenario('owned')
  await page.goto(base + '/')
  const link = page.locator('.factory-line [data-line-issue="1:610"]')
  await link.waitFor({ state: 'attached' })
  assert.match(await link.textContent(), /Blocked · Someone else is working on this issue/)
  assert.equal(await page.locator('#owned, #waiting, #needs-attention').count(), 0)
  for (const width of [390, 1440]) {
    await page.setViewportSize({ width, height: 1000 })
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    await page.screenshot({ path: `/tmp/forge-135-owned-${width}.png`, fullPage: true, animations: 'disabled' })
  }
  // Its cube opens the same panel a card did.
  await page.waitForFunction(() => document.querySelector('.factory-line [data-line-issue="1:610"]')?.hasAttribute('data-x'), null, { timeout: 10000 })
  await page.locator('.factory-line canvas[role="img"]').click({
    position: { x: Number(await link.getAttribute('data-x')), y: Number(await link.getAttribute('data-y')) },
  })
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
