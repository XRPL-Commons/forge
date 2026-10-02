import assert from 'node:assert/strict'

const CARDS = '#completed .issue-card'

function completedReads(requests) {
  return requests().then(reads => reads.filter(r => r.path === '/api/factory' && r.query.section === 'completed'))
}

async function titles(page) {
  return page.locator(`${CARDS} h3`).allInnerTexts()
}

// Recently completed on Activity: ten issues at a time, newest first, the next
// ten when the operator scrolls to the bottom, and a plain end. The fixture
// holds 25 completed issues stored oldest first, so the order is the paging's.
export default async function (page, { base, scenario, requests }) {
  await scenario('completed-many')
  await page.setViewportSize({ width: 1280, height: 700 })
  await page.goto(base + '/')
  await page.locator('#completed-title').waitFor()
  assert.equal((await page.locator('#completed-title').innerText()).trim(), 'Recently completed · 25')

  // The first page: ten cards, newest first, asked for as one page of ten.
  await page.waitForFunction((selector) => document.querySelectorAll(selector).length === 10, CARDS, { timeout: 10000 })
  await page.waitForTimeout(400)
  assert.equal(await page.locator(CARDS).count(), 10, 'only the first page loads before scrolling')
  const first = await titles(page)
  assert.deepEqual(first.slice(0, 3), ['Completed issue 25', 'Completed issue 24', 'Completed issue 23'])
  assert.equal(first.at(-1), 'Completed issue 16')
  let reads = await completedReads(requests)
  assert.equal(reads.length, 1)
  assert.deepEqual(reads[0].query, { section: 'completed', limit: '10' })

  // Every card is a cartoon: a merged cube wears its bow, a closed one does not.
  const merged = page.locator(`${CARDS}[data-key="1:825"]`)
  assert.equal(await merged.locator('canvas').getAttribute('aria-hidden'), 'true')
  assert.match(await merged.locator('canvas').getAttribute('data-look'), /^merged happy bow confetti$/)
  assert.equal((await merged.locator('[data-status-chip]').innerText()).trim(), 'Merged')
  assert.match(await merged.locator('[data-fact="age"]').innerText(), /^Merged .+ ago$/)
  const closed = page.locator(`${CARDS}[data-key="1:821"]`)
  assert.doesNotMatch(await closed.locator('canvas').getAttribute('data-look'), /bow|confetti/)
  assert.match(await closed.locator('[data-fact="age"]').innerText(), /^Closed .+ ago$/)
  assert.equal(await page.locator('#completed-end').count(), 0)

  // Scrolling to the bottom loads the next ten, from the first page's cursor.
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight))
  await page.waitForFunction((selector) => document.querySelectorAll(selector).length === 20, CARDS, { timeout: 10000 })
  reads = await completedReads(requests)
  assert.equal(reads.length, 2)
  assert.equal(reads[1].query.before, `${1789990000 + 16 * 60}:1:816`)
  const second = await titles(page)
  assert.deepEqual(second.slice(10, 12), ['Completed issue 15', 'Completed issue 14'])

  // …then the last five, and the end of the list says so.
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight))
  await page.locator('#completed-end').waitFor({ timeout: 10000 })
  assert.equal(await page.locator(CARDS).count(), 25)
  assert.equal((await page.locator('#completed-end').innerText()).trim(), "That's everything.")
  assert.equal((await titles(page)).at(-1), 'Completed issue 01')
  assert.equal(await page.locator('[data-completed-sentinel]').count(), 0)
  await page.waitForTimeout(300)
  assert.equal((await completedReads(requests)).length, 3, 'nothing more is asked for after the last page')

  // A completed card opens its panel like any other card.
  await page.locator(`${CARDS}[data-key="1:801"]`).click()
  await page.locator('[role="dialog"][data-side="right"]').waitFor({ timeout: 10000 })
  assert.equal(new URL(page.url()).searchParams.get('issue'), '1:801')
  await page.keyboard.press('Escape')

  // Without the observer's help (a tall list, a narrow viewport), Load more
  // fetches the next page by hand.
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(base + '/')
  await page.waitForFunction((selector) => document.querySelectorAll(selector).length >= 10, CARDS, { timeout: 10000 })
  const before = await page.locator(CARDS).count()
  const more = page.getByRole('button', { name: 'Load more' })
  await more.waitFor({ state: 'attached', timeout: 10000 })
  // Clicked in place: scrolling to it would let the observer load the page first.
  await more.evaluate(button => button.click())
  await page.waitForFunction(([selector, count]) => document.querySelectorAll(selector).length > count, [CARDS, before], { timeout: 10000 })
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'completed cards overflow at 390px')
  await scenario('default')
}
