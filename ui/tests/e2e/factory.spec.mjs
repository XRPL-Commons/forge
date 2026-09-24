import assert from 'node:assert/strict'

const ORPHAN_RECOVERY_JOB = '33333333-3333-4333-8333-333333333333'
const GITHUB_WARNING = 'GitHub observation or publication is unconfirmed'
// The detail panel is the slideover on the right; a stack deck and a
// confirmation are ordinary modals, so the three never collide in a locator.
const PANEL = '[role="dialog"][data-side="right"]'
const MODAL = '[role="dialog"]:not([data-side="right"])'
const CONFIRM = '[role="dialog"]:has(#confirm-reason)'
// The issue the mock serves for every /api/issues?issue= request.
const DETAIL_TITLE = 'Make course navigation easier'

// Counts how many times a string appears in the rendered (visible) page text.
function occurrences(page, text) {
  return page.evaluate((needle) => document.body.innerText.split(needle).length - 1, text)
}

function waitForText(page, text) {
  return page.waitForFunction((needle) => document.body.innerText.includes(needle), text, { timeout: 10000 })
}

// Opens a stack and waits for its modal to hold still: the dialog animates in
// with a scale, so a card measured too early is smaller than it renders.
async function openStack(page, stack) {
  await stack.click()
  const dialog = page.locator(MODAL)
  await dialog.locator('.issue-card').first().waitFor({ timeout: 10000 })
  await page.waitForFunction((selector) => {
    const dialog = document.querySelector(selector)
    if (!dialog || dialog.getAnimations().some((animation) => animation.playState === 'running')) return false
    const card = dialog.querySelector('.issue-card')
    return !!card && Math.abs(card.getBoundingClientRect().height - card.offsetHeight) < 0.05
  }, MODAL, { timeout: 10000 })
  return dialog
}

// The panel's own open/close, used from every section below.
async function openPanel(page, card) {
  await card.click()
  const panel = page.locator(PANEL)
  await panel.waitFor({ timeout: 10000 })
  return panel
}

async function closePanel(page) {
  await page.keyboard.press('Escape')
  await page.locator(PANEL).waitFor({ state: 'hidden', timeout: 10000 })
}

function query(page, name) {
  return new URL(page.url()).searchParams.get(name)
}

async function closeStack(page, dialog) {
  await page.keyboard.press('Escape')
  await dialog.waitFor({ state: 'hidden', timeout: 10000 })
}

// End-to-end behaviour of the Activity (Factory) page: one fixed card shape,
// one detail route per card, a single message block above the metrics, the
// stop confirmation round-trip, column moves and the reset fence.
export default async function (page, { base, scenario, requests }) {
  await page.goto(base + '/')
  await waitForText(page, 'Publish build logs without leaking install tokens')

  // Every card is the same height, although the default fixture deliberately
  // mixes a >120-character title, a queued issue with no job and no reason, a
  // prerequisite hold, a failure count, a job with provider + model and a
  // recovery job with no issue row.
  // Queued issues wait as one stack per column; only cards with a worker are
  // on the board until the operator opens the stack, which is a modal.
  assert.equal(await page.locator('#stage-build [data-key="1:611"]').count(), 0, 'a queued issue is folded into the stack')
  const stacks = page.locator('.issue-stack')
  assert.equal(await stacks.count(), 3, 'one stack per column that has queued work')
  assert.match(await stacks.first().innerText(), /\d+ queued/)

  // The heights of every card the fixture renders: the ones on the board,
  // then the ones each stack opens.
  const heights = []
  const boardCards = page.locator('.issue-card:visible')
  const boardCount = await boardCards.count()
  for (let index = 0; index < boardCount; index += 1) heights.push((await boardCards.nth(index).boundingBox()).height)
  for (let index = 0; index < 3; index += 1) {
    const dialog = await openStack(page, stacks.nth(index))
    const stacked = dialog.locator('.issue-card')
    const stackedCount = await stacked.count()
    for (let card = 0; card < stackedCount; card += 1) heights.push((await stacked.nth(card).boundingBox()).height)
    if (index === 0) {
      assert.equal(await dialog.locator('[data-key="1:611"]').isVisible(), true, 'the Build stack opens its queued cards')
      // The card itself is the control, in the modal as on the board.
      const queued = dialog.locator('[data-key="1:611"]')
      assert.equal(await queued.getAttribute('role'), 'link')
      assert.equal(await queued.getAttribute('aria-label'), 'Open XRPL-Commons/academy #611')
      assert.equal(await queued.locator('button').count(), 0, 'a card carries no button of its own')
    }
    if (index === 1) {
      // The >120-character title is clamped inside its fixed-size card.
      const titleBox = await dialog.locator('[data-key="1:612"] h3').evaluate((el) => ({
        scrollHeight: el.scrollHeight, clientHeight: el.clientHeight, text: el.textContent.trim(),
      }))
      assert.ok(titleBox.scrollHeight <= titleBox.clientHeight, `title overflows its box: ${JSON.stringify(titleBox)}`)
      assert.ok(titleBox.text.endsWith('…'), `expected a clamped title, saw ${titleBox.text}`)
    }
    await closeStack(page, dialog)
  }
  assert.equal(heights.length, 11, `expected the 11 cards of the default fixture, saw ${heights.length}`)
  assert.equal(new Set(heights).size, 1, `cards differ in height: ${heights.join(', ')}`)

  // Each breakdown row is a stat box that links to its section and carries
  // the derived sentence as its tooltip, not as a line that reflows.
  const attentionBox = page.locator('[data-breakdown="attention"]')
  assert.equal(await attentionBox.getAttribute('href'), '#needs-attention')
  assert.match(await attentionBox.innerText(), /Needs a decision/)
  assert.match(await attentionBox.innerText(), /awaiting you/)
  assert.equal(await attentionBox.getAttribute('title'), '2 failed')

  // The worker-output popover is gone; logs live in the detail view.
  // A worker busy on an issue is shown by its card, never repeated as a
  // status line above the boxes (the default operator status is "Building issue #601").
  assert.equal(await page.getByRole('link', { name: 'View issue' }).count(), 0)
  assert.equal(await occurrences(page, 'Building issue #601'), 0)
  assert.equal(await page.getByRole('button', { name: 'Output' }).count(), 0)
  assert.equal(await page.getByRole('link', { name: 'Output' }).count(), 0)

  // Nothing on the board is a button any more: the card is the control, and
  // clicking it opens the issue's detail in a panel over the right.
  assert.equal(await page.locator('.issue-card button').count(), 0, 'no card renders a button')
  const card = page.locator('[data-key="1:601"]')
  assert.equal(await card.getAttribute('role'), 'link')
  assert.equal(await card.getAttribute('aria-label'), 'Open XRPL-Commons/academy #601')
  let panel = await openPanel(page, card)
  await waitForText(page, DETAIL_TITLE)
  assert.equal(query(page, 'issue'), '1:601', 'the open panel is carried by the URL')
  // Escape closes it and leaves the URL clean.
  await closePanel(page)
  assert.equal(new URL(page.url()).search, '', `the closed panel left the URL dirty: ${page.url()}`)
  // …and focus is handed back to the card that opened it.
  await page.waitForFunction(
    () => document.activeElement?.getAttribute('data-key') === '1:601',
    null, { timeout: 5000 },
  )

  // …so a reload of that URL opens the same panel, and the board keeps
  // polling behind it.
  await page.goto(base + '/?issue=1%3A601')
  await page.locator(PANEL).waitFor({ timeout: 10000 })
  await waitForText(page, DETAIL_TITLE)
  await closePanel(page)

  // The standalone page is unchanged: the same detail, no overlay.
  await page.goto(base + '/issues/1%3A601')
  await waitForText(page, DETAIL_TITLE)
  assert.equal(await page.locator('[role="dialog"]').count(), 0, 'the standalone page renders no overlay')

  // An internal run opens the same panel at its own job, and its Reset moved
  // in with it: the board's confirmation, the board's payload.
  await page.goto(base + '/')
  await waitForText(page, 'Investigations and cleanup')
  panel = await openPanel(page, page.locator('#stage-recovery [data-key="3:900"]'))
  assert.equal(query(page, 'issue'), '3:900')
  assert.equal(query(page, 'job'), ORPHAN_RECOVERY_JOB, 'an internal card opens at its own run')
  await panel.getByRole('button', { name: 'Reset runner' }).click()
  const resetDialog = page.locator(CONFIRM)
  await resetDialog.waitFor({ timeout: 10000 })
  const confirmReset = resetDialog.getByRole('button', { name: 'Reset runner' })
  assert.equal(await confirmReset.isDisabled(), true, 'the confirm button must require a reason')
  await resetDialog.getByRole('textbox', { name: 'Decision or reason' }).fill('Runner wedged')
  await confirmReset.click()
  await resetDialog.waitFor({ state: 'hidden', timeout: 10000 })
  const reset = (await requests()).filter((entry) => entry.method === 'POST' && entry.path === '/api/factory/action')
  assert.equal(reset.length, 1)
  assert.deepEqual(reset[0].body, { action: 'reset', target: ORPHAN_RECOVERY_JOB, reason: 'Runner wedged' })
  await closePanel(page)

  // A supervisor observation error is one banner that absorbs the operator
  // status line: each sentence is rendered exactly once, and its button
  // points at the remediation for that phase.
  await scenario('warning')
  await waitForText(page, GITHUB_WARNING)
  assert.equal(await occurrences(page, GITHUB_WARNING), 1)
  assert.equal(await occurrences(page, 'Forge needs attention'), 1)
  const warningAction = page.getByRole('link', { name: 'Open GitHub access' })
  assert.equal(await warningAction.count(), 1)
  assert.equal(await warningAction.getAttribute('href'), '/settings/github')

  // Stop lives in the panel's action bar now: it asks for a decision, then
  // posts exactly {action, target, reason} against the issue's own attempt.
  await scenario('default')
  await page.goto(base + '/')
  await waitForText(page, 'Publish build logs without leaking install tokens')
  panel = await openPanel(page, page.locator('[data-key="1:601"]'))
  await panel.getByRole('button', { name: 'Stop attempt' }).click()
  const stopDialog = page.locator(CONFIRM)
  await stopDialog.waitFor({ timeout: 10000 })
  await waitForText(page, 'Stop attempt?')
  const confirmStop = stopDialog.getByRole('button', { name: 'Stop attempt' })
  assert.equal(await confirmStop.isDisabled(), true, 'the confirm button must require a reason')
  await stopDialog.getByRole('textbox', { name: 'What changed?' }).fill('Stuck for an hour')
  assert.equal(await confirmStop.isDisabled(), false)
  await confirmStop.click()
  await waitForText(page, 'Decision submitted. Waiting for confirmation.')

  const posted = (await requests()).filter((entry) => entry.method === 'POST' && entry.path === '/api/factory/action')
  assert.equal(posted.length, 1)
  assert.deepEqual(posted[0].body, { action: 'stop', target: 'attempt', reason: 'Stuck for an hour' })
  await closePanel(page)

  // A rejected operator request comes back through the next poll as a banner.
  await scenario('stop-rejected')
  await waitForText(page, 'Action could not be applied: Worker already stopped')
  assert.ok(await page.getByText('Action could not be applied: Worker already stopped').isVisible())

  // A card that changes stage moves column and is announced.
  await scenario('moved')
  // It arrives queued, so it lands in Review's stack; open the stack to see its card.
  await page.locator('#stage-review .issue-stack').waitFor({ timeout: 10000 })
  const movedDialog = await openStack(page, page.locator('#stage-review .issue-stack'))
  await movedDialog.locator('[data-key="1:601"]').waitFor({ timeout: 10000 })
  await closeStack(page, movedDialog)
  // The leaving node is animated out (420 ms), then gone from the old column.
  await page.locator('#stage-build [data-key="1:601"]').waitFor({ state: 'detached', timeout: 10000 })
  await page.waitForFunction(
    () => document.querySelector('p.sr-only')?.textContent.trim() === 'Issue 601 moved to Review',
    null, { timeout: 10000 },
  )

  // A reset fence replaces the last known state on the next poll alone: one
  // banner, no metrics, no columns, no cards — and it lifts the same way.
  await scenario('paused')
  await waitForText(page, 'Factory reset is in progress')
  assert.equal(await occurrences(page, 'Factory reset is in progress'), 1)
  assert.equal(await page.locator('.issue-card').count(), 0)
  assert.equal(await page.locator('#columns').count(), 0)
  assert.equal(await occurrences(page, 'Waiting for a worker'), 0)

  await scenario('default')
  await page.waitForFunction(() => document.querySelectorAll('.issue-card').length > 0, null, { timeout: 10000 })

  // A first load that never answers stays on "Connecting…" — there is no
  // last known state to reconnect to — and renders no metrics.
  await scenario('down')
  await page.goto(base + '/')
  await waitForText(page, 'Forge status service is unavailable')
  assert.equal((await page.locator('#freshness').innerText()).trim(), 'Connecting…')
  assert.equal(await occurrences(page, 'Waiting for a worker'), 0)
  assert.equal(await page.locator('#columns').count(), 0)
  assert.equal(await page.locator('.issue-card').count(), 0)

  // Eleven queued issues stay one card and one deck: opening the deck lists
  // all eleven in a modal, and the board itself does not grow.
  await scenario('stacked')
  await page.goto(base + '/')
  await waitForText(page, '11 queued')
  const compact = (await page.locator('#columns').boundingBox()).height
  const queuedDialog = await openStack(page, page.locator('#stage-build .issue-stack'))
  await waitForText(page, '11 queued · Build')
  assert.equal(await queuedDialog.locator('.issue-card').count(), 11, 'the stack lists every queued issue')
  assert.equal(await page.locator('#stage-build .issue-card').count(), 1, 'the column keeps only the working card')
  assert.equal((await page.locator('#columns').boundingBox()).height, compact, 'opening a stack must not grow the board')
  // A card opened from inside the deck replaces it: the modal closes, the
  // panel takes its place, and the URL carries the issue.
  const stacked = queuedDialog.locator('.issue-card').first()
  const stackedKey = await stacked.getAttribute('data-key')
  await stacked.click()
  await queuedDialog.waitFor({ state: 'hidden', timeout: 10000 })
  await page.locator(PANEL).waitFor({ timeout: 10000 })
  await waitForText(page, DETAIL_TITLE)
  assert.equal(query(page, 'issue'), stackedKey)
  await closePanel(page)

  // Needs attention keeps the decisions expanded and stacks the holds, while
  // its header counts both.
  const attention = page.locator('#needs-attention')
  assert.equal((await attention.locator('h2').innerText()).trim(), 'Needs attention · 5')
  assert.equal(await attention.locator('.issue-card').count(), 2, 'only the decisions waiting on the operator are cards')
  const holdStack = attention.locator('.issue-stack')
  assert.match(await holdStack.innerText(), /3 on hold/)
  const holdDialog = await openStack(page, holdStack)
  assert.equal(await holdDialog.locator('.issue-card').count(), 3)
  assert.match(await holdDialog.innerText(), /3 on hold/)
  await closeStack(page, holdDialog)

  // On a phone-width viewport the cards fill the width and nothing scrolls
  // sideways.
  await scenario('default')
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(base + '/')
  await waitForText(page, 'Publish build logs without leaking install tokens')
  const width = (await page.locator('[data-key="1:601"]').boundingBox()).width
  assert.ok(width >= 300, `card is ${width}px wide at 390px viewport`)
  const overflow = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth,
  }))
  assert.ok(overflow.scrollWidth <= overflow.innerWidth, `page scrolls sideways: ${JSON.stringify(overflow)}`)

  // …and the panel is the whole screen there, still without a sideways scroll.
  panel = await openPanel(page, page.locator('[data-key="1:601"]'))
  await waitForText(page, DETAIL_TITLE)
  const panelBox = await panel.boundingBox()
  assert.ok(panelBox.width >= 380, `the panel is ${panelBox.width}px wide at 390px viewport`)
  const panelOverflow = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth,
  }))
  assert.ok(
    panelOverflow.scrollWidth <= panelOverflow.innerWidth,
    `the open panel scrolls the page sideways: ${JSON.stringify(panelOverflow)}`,
  )
  await closePanel(page)
}
