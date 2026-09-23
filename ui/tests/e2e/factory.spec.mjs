import assert from 'node:assert/strict'

const BUILD_JOB = '11111111-1111-4111-8111-111111111111'
const ORPHAN_RECOVERY_JOB = '33333333-3333-4333-8333-333333333333'
const GITHUB_WARNING = 'GitHub observation or publication is unconfirmed'

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
  const dialog = page.getByRole('dialog')
  await dialog.locator('.issue-card').first().waitFor({ timeout: 10000 })
  await page.waitForFunction(() => {
    const dialog = document.querySelector('[role="dialog"]')
    if (!dialog || dialog.getAnimations().some((animation) => animation.playState === 'running')) return false
    const card = dialog.querySelector('.issue-card')
    return !!card && Math.abs(card.getBoundingClientRect().height - card.offsetHeight) < 0.05
  }, null, { timeout: 10000 })
  return dialog
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
      // Stop / open actions survive the move into the modal.
      assert.equal(await dialog.locator('[data-key="1:611"]').getByRole('link', { name: 'Open' }).count(), 1)
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
  assert.equal(await page.getByRole('button', { name: 'Output' }).count(), 0)
  assert.equal(await page.getByRole('link', { name: 'Output' }).count(), 0)

  // One detail view per card: /issues/<key>, and ?job=<id> for a worker row.
  const openIssue = page.locator('[data-key="1:601"]').getByRole('link', { name: 'Open' })
  assert.equal(await openIssue.getAttribute('href'), '/issues/1%3A601')
  await openIssue.click()
  await page.waitForURL(base + '/issues/1%3A601')

  await page.goBack()
  await waitForText(page, 'Investigations and cleanup')
  const openJob = page.locator(`#stage-recovery [data-key="3:900"]`).getByRole('link', { name: 'Open' })
  assert.equal(await openJob.getAttribute('href'), `/issues/3%3A900?job=${ORPHAN_RECOVERY_JOB}`)
  await openJob.click()
  await page.waitForURL(`${base}/issues/3%3A900?job=${ORPHAN_RECOVERY_JOB}`)
  await page.goBack()

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

  // Stop asks for a decision, then posts exactly {action, target, reason}.
  await scenario('default')
  await page.goto(base + '/')
  await waitForText(page, 'Publish build logs without leaking install tokens')
  await page.locator('[data-key="1:601"]').getByRole('button', { name: 'Stop' }).click()
  await waitForText(page, 'Stop this attempt?')
  const confirmStop = page.getByRole('button', { name: 'Stop attempt' })
  assert.equal(await confirmStop.isDisabled(), true, 'the confirm button must require a reason')
  await page.getByRole('textbox', { name: 'Decision or reason' }).fill('Stuck for an hour')
  assert.equal(await confirmStop.isDisabled(), false)
  await confirmStop.click()
  await waitForText(page, 'Action queued for the supervisor.')

  const posted = (await requests()).filter((entry) => entry.method === 'POST' && entry.path === '/api/factory/action')
  assert.equal(posted.length, 1)
  assert.deepEqual(posted[0].body, { action: 'stop', target: BUILD_JOB, reason: 'Stuck for an hour' })

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
  await closeStack(page, queuedDialog)

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
}
