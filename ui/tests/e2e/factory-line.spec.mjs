import assert from 'node:assert/strict'

const STRIP = '.factory-line canvas[role="img"]'
const PANEL = '[role="dialog"][data-side="right"]'

async function headline(page) {
  const canvas = page.locator(STRIP)
  await canvas.waitFor({ timeout: 10000 })
  return canvas.getAttribute('aria-label')
}

// The factory line strip at the top of Activity: its accessible name is the
// headline it draws, it fits a phone, and a cube opens its issue.
export default async function (page, { base, scenario }) {
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.goto(base + '/')
  // Default fixture: two failed issues, one build of four slots in use.
  assert.equal(await headline(page), '2 need you · 1 of 4 slots working')

  await scenario('factory-line')
  await page.goto(base + '/')
  await page.waitForFunction((selector) => document.querySelector(selector)?.getAttribute('aria-label')?.includes('bottleneck'), STRIP, { timeout: 10000 })
  assert.equal(await headline(page), '5 need you · Build is the bottleneck, 3 waiting')
  const summary = await page.locator('.factory-line [data-line-summary]').textContent()
  assert.match(summary, /Queued 6, Build 2 of 2, Review 0 of 1, Audit 0 of 1/)
  assert.match(summary, /need you 5, waiting on other issues 1, on hold 1\./)
  // Every cube has a hidden link for screen readers, named like its tooltip.
  assert.equal(await page.locator('.factory-line [data-line-issue="2:616"]').textContent(), '#616 · Needs attention · 2 of 3 attempts failed · XRPL-Commons/hackathon')

  const box = await page.locator(STRIP).boundingBox()
  assert.equal(Math.round(box.height), 200)
  await page.waitForTimeout(1200)
  await page.screenshot({ path: '/tmp/forge-factory-line-desktop.png', clip: { x: 0, y: 0, width: 1280, height: Math.ceil(box.y + box.height + 160) } })

  // Clicking a cube on the canvas opens that issue's panel, as a card does.
  const link = page.locator('.factory-line [data-line-issue="1:604"]')
  await page.waitForFunction(() => document.querySelector('.factory-line [data-line-issue="1:604"]')?.hasAttribute('data-x'), null, { timeout: 10000 })
  const x = Number(await link.getAttribute('data-x'))
  const y = Number(await link.getAttribute('data-y'))
  await page.locator(STRIP).click({ position: { x, y } })
  await page.locator(PANEL).waitFor({ timeout: 10000 })
  assert.equal(new URL(page.url()).searchParams.get('issue'), '1:604')
  await page.keyboard.press('Escape')
  await page.locator(PANEL).waitFor({ state: 'hidden', timeout: 10000 })

  // A phone: the strip keeps its height and the page never scrolls sideways.
  await page.setViewportSize({ width: 390, height: 844 })
  await page.waitForTimeout(600)
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'factory line overflows at 390px')
  const phone = await page.locator(STRIP).boundingBox()
  assert.ok(phone.width <= 390 && phone.width > 300, `strip width ${phone.width}`)
  assert.equal(Math.round(phone.height), 200)
  await page.evaluate(() => scrollTo(0, 0))
  await page.screenshot({ path: '/tmp/forge-factory-line-mobile.png' })

  // With reduced motion the strip is a still frame with the same name.
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.reload()
  assert.equal(await headline(page), '5 need you · Build is the bottleneck, 3 waiting')
  await page.emulateMedia({ reducedMotion: null })
}
