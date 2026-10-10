import assert from 'node:assert/strict'

const STRIP = '.factory-line canvas[role="img"]'

async function spot(page, key) {
  const link = page.locator(`.factory-line [data-line-issue="${key}"]`)
  await page.waitForFunction((k) => document.querySelector(`.factory-line [data-line-issue="${k}"]`)?.hasAttribute('data-x'), key, { timeout: 10000 })
  return { x: Number(await link.getAttribute('data-x')), y: Number(await link.getAttribute('data-y')) }
}

// #241: an issue whose review result is being checked (result_pending) is the
// review stage still at work. It rests at the review machine, reads Working,
// and is neither queued nor on hold; a paused issue still is.
export default async function (page, { base, scenario }) {
  await page.setViewportSize({ width: 1280, height: 900 })
  await scenario('checking')
  await page.goto(base + '/')
  await page.locator(STRIP).waitFor({ timeout: 10000 })

  assert.equal(await page.locator('.factory-line [data-line-issue="1:701"]').textContent(),
    '#701 · Working · Checking the completed worker result')
  const checking = await spot(page, '1:701'), reviewing = await spot(page, '1:702'), paused = await spot(page, '1:703')
  // Same machine row and bay as the running review, not the hold bench.
  assert.equal(checking.y, reviewing.y)
  assert.ok(Math.abs(checking.x - reviewing.x) < 40, `checking cube ${JSON.stringify(checking)} is not at review ${JSON.stringify(reviewing)}`)
  assert.ok(paused.y > checking.y)

  const summary = await page.locator('.factory-line [data-line-summary]').textContent()
  assert.match(summary, /^Queued 0, /)
  assert.match(summary, /on hold 1\./)
  assert.match((await page.locator('#readout [data-readout="queued"]').innerText()).replace(/\s+/g, ' '), /Queued 0/)
  assert.match((await page.locator('#readout [data-readout="attention"]').innerText()).replace(/\s+/g, ' '), /Needs you 0/)
}
