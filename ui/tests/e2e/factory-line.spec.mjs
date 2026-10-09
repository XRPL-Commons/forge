import assert from 'node:assert/strict'

const STRIP = '.factory-line canvas[role="img"]'
const PANEL = '[role="dialog"][data-side="right"]'
const NEEDS = '[role="dialog"]:has([data-needs-you])'
const CARD = '[data-line-card]'

async function headline(page) {
  const canvas = page.locator(STRIP)
  await canvas.waitFor({ timeout: 10000 })
  return canvas.getAttribute('aria-label')
}

// Where a cube rests on the canvas, from its hidden link.
async function spot(page, key) {
  const link = page.locator(`.factory-line [data-line-issue="${key}"]`)
  await page.waitForFunction((k) => document.querySelector(`.factory-line [data-line-issue="${k}"]`)?.hasAttribute('data-x'), key, { timeout: 10000 })
  return { x: Number(await link.getAttribute('data-x')), y: Number(await link.getAttribute('data-y')) }
}

// A region's box relative to the canvas (the crate, or one bay).
async function region(page, selector) {
  const [box, canvas] = await Promise.all([page.locator(selector).boundingBox(), page.locator(STRIP).boundingBox()])
  return { x: box.x - canvas.x, y: box.y - canvas.y, w: box.width, h: box.height }
}

async function settled(page, selector) {
  const dialog = page.locator(selector)
  await dialog.waitFor({ timeout: 10000 })
  await page.waitForFunction((s) => {
    const el = document.querySelector(s)
    return !!el && !el.getAnimations({ subtree: true }).some(a => a.playState === 'running')
  }, selector, { timeout: 10000 })
  return dialog
}

const keys = (dialog) => dialog.locator('.issue-card').evaluateAll(cards => cards.map(card => card.getAttribute('data-key')))

// The factory line strip at the top of Activity: its accessible name is the
// headline it draws, it fits a phone, a cube opens its issue, the crate opens
// the list of issues that need the operator, and a hover card explains a
// cube without ever being clipped.
export default async function (page, { base, scenario }) {
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.goto(base + '/')
  // Default fixture: two failed issues, one build of four slots in use.
  assert.equal(await headline(page), '2 need you · 1 of 4 slots in use')

  await scenario('factory-line')
  await page.goto(base + '/')
  await page.waitForFunction((selector) => document.querySelector(selector)?.getAttribute('aria-label')?.includes('bottleneck'), STRIP, { timeout: 10000 })
  assert.equal(await headline(page), '5 need you · Build is the bottleneck, 3 waiting')
  const summary = await page.locator('.factory-line [data-line-summary]').textContent()
  assert.match(summary, /Queued 6, Build 2 of 2, Review 0 of 1, Audit 0 of 1/)
  assert.match(summary, /need you 5, waiting on other issues 4, on hold 1\./)
  // Every cube has a hidden link for screen readers, named like its tooltip.
  assert.equal(await page.locator('.factory-line [data-line-issue="2:616"]').textContent(), '#616 · Needs attention · An attempt failed · XRPL-Commons/hackathon')
  // The page has no Needs attention section and no "on hold" stack.
  assert.equal(await page.locator('#needs-attention, .issue-stack').count(), 0)

  const box = await page.locator(STRIP).boundingBox()
  assert.equal(Math.round(box.height), 200)
  await page.waitForTimeout(1200)
  await page.screenshot({ path: '/tmp/forge-factory-line-desktop.png', clip: { x: 0, y: 0, width: 1280, height: Math.ceil(box.y + box.height + 160) } })

  // Hovering a cube shows a card: where it lives, its title, what it needs,
  // its stage and what it unblocks. The leftmost cube's card stays on screen.
  const left = await spot(page, '1:722')
  await page.mouse.move(box.x + left.x, box.y + left.y - 4)
  const card = page.locator(CARD)
  await page.waitForFunction((s) => document.querySelector(s)?.style.visibility === 'visible', CARD, { timeout: 5000 })
  const text = await card.innerText()
  assert.match(text, /XRPL-Commons\/academy · #722/)
  assert.match(text, /Rework the certificate flow/)
  assert.match(text, /Out of attempts: describe a different approach, then retry/)
  assert.match(text, /Needs attention/)
  assert.match(text, /Unblocks 2/)
  assert.match(text, /Click to open/)
  const cardBox = await card.boundingBox()
  assert.ok(cardBox.x >= 8 && cardBox.x + cardBox.width <= 1280 - 8, `hover card ${JSON.stringify(cardBox)} leaves the viewport`)
  assert.ok(cardBox.y >= 0 && cardBox.y + cardBox.height <= 900, `hover card ${JSON.stringify(cardBox)} leaves the viewport`)
  assert.ok(cardBox.width <= 281, `hover card is ${cardBox.width}px wide`)
  assert.equal(await card.evaluate(el => getComputedStyle(el).position), 'fixed')
  assert.equal(await page.locator(STRIP).evaluate(el => el.style.cursor), 'pointer')

  // Clicking a cube on the canvas opens that issue's panel, as a card does.
  const cube = await spot(page, '1:604')
  await page.locator(STRIP).click({ position: cube })
  await page.locator(PANEL).waitFor({ timeout: 10000 })
  assert.equal(new URL(page.url()).searchParams.get('issue'), '1:604')
  await page.keyboard.press('Escape')
  await page.locator(PANEL).waitFor({ state: 'hidden', timeout: 10000 })

  // The crate (here its "N NEED YOU" end) opens every issue that needs the
  // operator, ranked by how many other issues each one unblocks.
  const crate = await region(page, '.factory-line [data-line-crate]')
  assert.equal(await page.locator('.factory-line [data-line-crate]').getAttribute('aria-label'), 'Show the 5 issues that need you')
  await page.locator(STRIP).click({ position: { x: crate.x + crate.w - 6, y: crate.y + crate.h - 6 } })
  let needs = await settled(page, NEEDS)
  assert.equal((await needs.locator('[data-needs-title]').innerText()).trim(), '5 need you')
  assert.deepEqual(await keys(needs), ['1:722', '1:604', '1:730', '2:616', '1:721'])
  assert.equal((await needs.locator('[data-key="1:722"] [data-card-tag]').innerText()).trim(), 'Unblocks 2')
  assert.equal((await needs.locator('[data-key="1:604"] [data-card-tag]').innerText()).trim(), 'Unblocks 1')
  assert.equal(await needs.locator('[data-key="1:730"] [data-card-tag]').count(), 0)
  assert.equal((await needs.locator('[data-needs-filter="all"]').innerText()).replace(/\s+/g, ' ').trim(), 'All 5')
  await page.keyboard.press('Escape')
  await needs.waitFor({ state: 'hidden' })

  // A bay opens the same list filtered to it; the chips switch filters.
  const retry = await region(page, '.factory-line [data-line-bay="failed"]')
  await page.locator(STRIP).click({ position: { x: retry.x + retry.w / 2, y: retry.y + 3 } })
  needs = await settled(page, NEEDS)
  assert.equal(await needs.locator('[data-needs-filter="failed"]').getAttribute('aria-pressed'), 'true')
  assert.deepEqual(await keys(needs), ['1:604', '2:616'])
  assert.equal((await needs.locator('[data-needs-reason]').innerText()).trim(), 'An attempt failed')
  await needs.locator('[data-needs-filter="all"]').click()
  assert.equal((await keys(needs)).length, 5)
  // A card replaces the list with that issue's panel.
  await needs.locator('[data-key="1:722"]').click()
  await needs.waitFor({ state: 'hidden', timeout: 10000 })
  await page.locator(PANEL).waitFor({ timeout: 10000 })
  assert.equal(new URL(page.url()).searchParams.get('issue'), '1:722')
  await page.keyboard.press('Escape')
  await page.locator(PANEL).waitFor({ state: 'hidden', timeout: 10000 })

  // The keyboard reaches the crate too, and its card says what is inside.
  await page.locator('.factory-line [data-line-crate]').focus()
  await page.waitForFunction((s) => document.querySelector(s)?.innerText.includes('5 need you'), CARD, { timeout: 5000 })
  await page.keyboard.press('Enter')
  needs = await settled(page, NEEDS)
  await page.keyboard.press('Escape')
  await needs.waitFor({ state: 'hidden' })

  // The readout's "Needs you" opens the same list.
  await page.locator('#readout [data-readout="attention"] button').click()
  needs = await settled(page, NEEDS)
  assert.equal((await keys(needs)).length, 5)
  await page.keyboard.press('Escape')
  await needs.waitFor({ state: 'hidden' })

  // A phone: the strip keeps its height and the page never scrolls sideways.
  await page.setViewportSize({ width: 390, height: 844 })
  await page.waitForTimeout(600)
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'factory line overflows at 390px')
  const phone = await page.locator(STRIP).boundingBox()
  assert.ok(phone.width <= 390 && phone.width > 300, `strip width ${phone.width}`)
  assert.equal(Math.round(phone.height), 200)
  await page.evaluate(() => scrollTo(0, 0))
  await page.screenshot({ path: '/tmp/forge-factory-line-mobile.png' })
  // The hover card stays inside a phone screen as well.
  const small = await spot(page, '1:722')
  const smallBox = await page.locator(STRIP).boundingBox()
  await page.mouse.move(smallBox.x + small.x, smallBox.y + small.y - 3)
  await page.waitForFunction((s) => document.querySelector(s)?.style.visibility === 'visible', CARD, { timeout: 5000 })
  const phoneCard = await page.locator(CARD).boundingBox()
  assert.ok(phoneCard.x >= 8 && phoneCard.x + phoneCard.width <= 390 - 8, `hover card ${JSON.stringify(phoneCard)} leaves a phone screen`)
  await page.mouse.move(1, 1)
  // The list fits a phone without a sideways scroll.
  await page.locator('#readout [data-readout="attention"] button').click()
  needs = await settled(page, NEEDS)
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'the list overflows at 390px')
  const listBox = await needs.boundingBox()
  assert.ok(listBox.x >= 0 && listBox.x + listBox.width <= 390, `the list is ${JSON.stringify(listBox)}`)
  await page.screenshot({ path: '/tmp/forge-needs-you-mobile.png' })
  await page.keyboard.press('Escape')
  await needs.waitFor({ state: 'hidden' })
  await page.setViewportSize({ width: 1280, height: 900 })

  // With reduced motion the strip is a still frame with the same name.
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.reload()
  assert.equal(await headline(page), '5 need you · Build is the bottleneck, 3 waiting')
  await page.emulateMedia({ reducedMotion: null })
}
