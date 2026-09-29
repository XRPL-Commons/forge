import assert from 'node:assert/strict'

const RECOVERY_JOB = '22222222-2222-4222-8222-222222222222'
// The mock serves one detail fixture per scenario (`issues.issue.json`): the
// key in the query picks the endpoint, never the payload. So a detail
// assertion is always about the payload that scenario serves — which is why
// the decision scenarios below exist, one per issue state that offers the
// decision being exercised.
const DETAIL_TITLE = 'Make course navigation easier'

function waitForText(page, text) {
  return page.waitForFunction((needle) => document.body.innerText.includes(needle), text, { timeout: 10000 })
}

function issueRequests(entries) {
  return entries.filter((entry) => entry.method === 'GET' && entry.path === '/api/issues')
}

async function waitUntil(condition, what, timeout = 10000) {
  const deadline = Date.now() + timeout
  for (;;) {
    const value = await condition()
    if (value) return value
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${what}`)
    await new Promise((resolve) => setTimeout(resolve, 100))
  }
}

// The mock records every request it answers; this waits for the app to have
// actually sent one, rather than for a repaint that may not mean it did.
function waitForRequest(requests, predicate, what) {
  return waitUntil(async () => {
    const found = (await requests()).filter(predicate)
    return found.length ? found : null
  }, what ?? 'a recorded request')
}

// End-to-end behaviour of the Issues list and the issue detail route: the
// filters the operator actually uses, one URL per issue, the worker output of
// any job the issue has had, and the decision round-trip.
export default async function (page, { base, scenario, requests }) {
  // Server-rendered text appears before Vue attaches; typing into an input
  // then is lost and its button stays disabled. Every load waits for hydration.
  const load = page.goto.bind(page)
  page.goto = async (...args) => {
    const response = await load(...args)
    await page.waitForFunction(() => {
      const app = document.querySelector('#__nuxt')?.__vue_app__
      return app?.config.globalProperties.$nuxt?.isHydrating === false
    }, undefined, { timeout: 15000 })
    return response
  }
  await page.goto(base + '/issues')
  await waitForText(page, 'Redact credential values at the logging boundary')

  // The list says what it is and that reading it is safe.
  assert.equal((await page.locator('h1').innerText()).trim(), 'Issues')
  await waitForText(page, 'Up to date · Viewing issues never queues them for work.')
  await waitForText(page, '6 issues · Page 1 of 1')

  // Each issue is its own card, and its one button says what opening it is for:
  // the decision it waits on, else its progress, else just opening it.
  assert.equal(await page.locator('article.issue-row').count(), 6)
  assert.equal(await page.locator('[data-key="1:610"]').getByRole('link', { name: 'View progress' }).count(), 1)
  assert.equal(await page.locator('[data-key="1:613"]').getByRole('link', { name: 'Recheck readiness' }).count(), 1)
  assert.equal(await page.locator('[data-key="1:609"]').getByRole('link', { name: 'Queue for Forge' }).count(), 1)
  assert.equal(await page.locator('[data-key="2:585"]').getByRole('link', { name: 'Release hold' }).count(), 1)
  assert.equal(await page.locator('[data-key="1:608"]').getByRole('link', { name: 'Open', exact: true }).count(), 1)
  assert.equal((await page.locator('[data-key="1:613"] [data-status-chip]').innerText()).trim(), 'Needs attention')
  assert.equal((await page.locator('[data-key="1:608"] [data-status-chip]').innerText()).trim(), 'Queued')

  // The card tells the story, then only the facts the row carries.
  const story = (await page.locator('[data-key="1:613"] [data-story]').innerText()).trim()
  assert.ok(story.startsWith('Paused until repository setup is checked again.'), story)
  const facts = async (key) => page.locator(`[data-key="${key}"] [data-fact]`).allInnerTexts()
  const redact = await facts('1:613')
  assert.deepEqual(redact.slice(0, 3), ['PR #88', 'Earlier Forge draft #71', '1 of 3 attempts failed'])
  assert.match(redact[3], /^Updated /)
  const held = await facts('2:585')
  assert.deepEqual(held.slice(0, 2), ['Depends on #584', 'On hold: hold'])
  assert.equal(await page.locator('[data-key="1:609"] [data-fact^="pr-"]').count(), 0, 'no pull request fact without a pull request')

  // The whole card opens the issue: a click on its story lands on the issue,
  // and the title link carries a visible focus ring for the keyboard.
  const storyText = page.locator('[data-key="1:609"] [data-story]')
  await storyText.scrollIntoViewIfNeeded()
  const cardBox = await storyText.boundingBox()
  await page.mouse.click(cardBox.x + 4, cardBox.y + 4)
  await page.waitForURL(base + '/issues/1%3A609')
  await page.goBack()
  await waitForText(page, 'Redact credential values at the logging boundary')
  const titleLink = page.locator('[data-key="1:613"] a.issue-link')
  await titleLink.focus()
  await page.keyboard.press('Shift+Tab')
  await page.keyboard.press('Tab')
  const ring = await titleLink.evaluate((el) => getComputedStyle(el, '::after').outlineStyle)
  assert.equal(ring, 'solid', 'a keyboard-focused card shows a focus ring')

  // The Show filter asks the API for that view.
  await page.getByRole('button', { name: 'Show' }).click()
  await page.getByRole('option', { name: 'Completed' }).click()
  await page.waitForFunction(
    () => document.querySelector('button[aria-label="Show"]')?.innerText.includes('Completed'),
    null, { timeout: 10000 },
  )
  const completed = await waitForRequest(requests, (entry) => entry.path === '/api/issues' && entry.query.state === 'completed')
  assert.ok(completed.length >= 1, 'the Show filter must reach the API')
  assert.equal(completed[0].query.page, '1')

  // Typing is debounced: only the settled search text is ever requested.
  await scenario('default')
  await page.goto(base + '/issues')
  await waitForText(page, 'Redact credential values at the logging boundary')
  await page.getByRole('textbox', { name: 'Find an issue' }).pressSequentially('logging', { delay: 30 })
  await waitForRequest(requests, (entry) => entry.path === '/api/issues' && entry.query.q === 'logging')
  const searched = issueRequests(await requests()).filter((entry) => entry.query.q !== undefined)
  assert.deepEqual([...new Set(searched.map((entry) => entry.query.q))], ['logging'],
    'a debounced search must never request a half-typed word')
  assert.deepEqual([...new Set(searched.map((entry) => entry.query.page))], ['1'], 'a new search starts at page 1')

  // A row is a link to the issue's own route.
  await scenario('default')
  await page.goto(base + '/issues')
  await waitForText(page, 'Redact credential values at the logging boundary')
  const rowLink = page.locator('[data-key="1:613"]').getByRole('link', { name: 'Redact credential values at the logging boundary' })
  assert.equal(await rowLink.getAttribute('href'), '/issues/1%3A613')
  await rowLink.click()
  await page.waitForURL(base + '/issues/1%3A613')
  await waitForText(page, DETAIL_TITLE)
  assert.equal((await page.locator('#issue-title').innerText()).trim(), DETAIL_TITLE)

  // The old `/issues?issue=<key>` shape — still handed to the UI in
  // `operator.action_url` — lands on that one route.
  await page.goto(base + '/issues?issue=1:613')
  await page.waitForURL(base + '/issues/1%3A613')
  await waitForText(page, DETAIL_TITLE)

  // The description is rendered server-side: no script survives, the
  // relative link points at the file on GitHub, the checkbox is disabled.
  // It starts collapsed, but is in the page from the first paint.
  assert.equal(await page.locator('.forge-markdown').isVisible(), false, 'the description starts collapsed')
  await page.getByRole('button', { name: 'Description' }).click()
  await page.locator('.forge-markdown').waitFor()
  const injected = await page.evaluate(() => window.injected ?? null)
  assert.equal(injected, null, 'a <script> in an issue body must never execute')
  assert.equal(await page.locator('.forge-markdown script').count(), 0)
  const specLink = page.getByRole('link', { name: 'the navigation spec' })
  assert.equal(await specLink.getAttribute('href'), 'https://github.com/XRPL-Commons/academy/blob/HEAD/docs/x.md')
  assert.equal(await specLink.getAttribute('target'), '_blank')
  assert.equal(await page.locator('.forge-markdown input[type="checkbox"]').isDisabled(), true)

  // Worker output is there for the working issue, loads when it is opened,
  // and shows the captured log as text — never as markup.
  await scenario('default')
  await page.goto(base + '/issues/1%3A610')
  await waitForText(page, DETAIL_TITLE)
  assert.equal(await page.locator('pre').count(), 0, 'output is loaded on expand, not on load')
  await page.getByRole('button', { name: 'Worker output' }).click()
  await waitForText(page, 'line <b>two</b>')
  const pre = page.locator('pre')
  assert.match(await pre.innerText(), /line one\nline <b>two<\/b>\nline three/)
  assert.equal(await pre.locator('b').count(), 0, 'captured output must be escaped, not rendered')
  const outputs = (await requests()).filter((entry) => entry.path === '/api/factory/output')
  assert.equal(outputs.length, 1)
  assert.equal(outputs[0].query.id, 'attempt', 'the issue’s active job is the default selection')

  // An open panel stays open across polls, and is not re-fetched by them.
  const before = issueRequests(await requests()).length
  await waitUntil(async () => issueRequests(await requests()).length > before + 1, 'two more polls')
  assert.equal(await pre.isVisible(), true, 'a poll must never collapse an open panel')
  assert.equal((await requests()).filter((entry) => entry.path === '/api/factory/output').length, 1)

  // The ledger is readable in the operator's words.
  await page.getByRole('button', { name: 'Recent activity' }).click()
  await waitForText(page, 'Added to Forge')
  await waitForText(page, 'Queued for Forge')

  // …and for a job the issue is not running: the recovery investigation of
  // 1:604 is reachable by its own link, output included.
  await scenario('default')
  await page.goto(`${base}/issues/1%3A604?job=${RECOVERY_JOB}`)
  await waitForText(page, DETAIL_TITLE)
  const jobs = page.locator('#issue-jobs li')
  // The job list fills in from client data after the title renders.
  await jobs.first().waitFor()
  assert.equal(await jobs.count(), 1, 'the issue lists its own jobs')
  assert.match(await jobs.first().innerText(), /^recovery · running/)
  assert.equal(await jobs.first().getByRole('link').getAttribute('aria-current'), 'true')
  await page.getByRole('button', { name: 'Worker output' }).click()
  await waitForText(page, 'line <b>two</b>')
  const recoveryOutputs = (await requests()).filter((entry) => entry.path === '/api/factory/output')
  assert.equal(recoveryOutputs.length, 1)
  assert.equal(recoveryOutputs[0].query.id, RECOVERY_JOB, 'the ?job= id is what is fetched')

  // …and when the issue itself is gone from the inventory, the ?job= link
  // still reaches that job's output. The alert says why the rest is missing;
  // losing the log with the issue would strand the operator.
  await scenario('orphan-job')
  await page.goto(`${base}/issues/9%3A999?job=${RECOVERY_JOB}`)
  await waitForText(page, 'This issue is not in a managed repository. Refresh the issue list.')
  assert.equal(await page.locator('#issue-title').count(), 0, 'there is no issue to show')
  await page.getByRole('button', { name: 'Worker output' }).click()
  await waitForText(page, 'orphan job line two')
  assert.match(await page.locator('pre').innerText(), /orphan job line one\norphan job line two/)
  const orphanOutputs = (await requests()).filter((entry) => entry.path === '/api/factory/output')
  assert.equal(orphanOutputs.length, 1)
  assert.equal(orphanOutputs[0].query.id, RECOVERY_JOB, 'the orphaned ?job= id is what is fetched')

  // The issue's one action bar sits under the title, above the description:
  // the decisions, then where the work lives on GitHub, then Stop. The review
  // summaries below it are clamped until "More" is pressed.
  await scenario('reviewed')
  await page.goto(base + '/issues/1%3A610')
  await waitForText(page, DETAIL_TITLE)
  const actionBar = page.locator('#issue-actions')
  assert.equal(await actionBar.getByRole('link', { name: 'GitHub' }).getAttribute('href'),
    'https://github.com/XRPL-Commons/academy/issues/610')
  assert.equal(await actionBar.getByRole('link', { name: 'PR #30' }).getAttribute('href'),
    'https://github.com/XRPL-Commons/academy/pull/30')
  assert.equal(await actionBar.getByRole('button', { name: 'Stop attempt' }).count(), 1)
  assert.equal(await page.getByRole('link', { name: 'Open on GitHub ↗' }).count(), 0, 'the links row at the bottom is gone')
  // Discussing the blocker is one more button in the same bar.
  assert.equal(await actionBar.getByRole('button', { name: 'Discuss blocker', exact: true }).count(), 1)
  await page.getByRole('button', { name: 'Description' }).click()
  await page.locator('.forge-markdown').waitFor()
  const barBox = await actionBar.boundingBox()
  const bodyBox = await page.locator('.forge-markdown').boundingBox()
  assert.ok(barBox.y + barBox.height <= bodyBox.y, 'the action bar must sit above the description')

  // The review rows live inside Details, open from the start, and each chip is
  // that reviewer's own verdict — a passed review reads "Passed" whatever the
  // issue's status is.
  await waitForText(page, 'The change is correct')
  const reviewRows = page.locator('#issue-reviews > div')
  assert.equal(await reviewRows.count(), 2)
  assert.match(await reviewRows.first().innerText(), /^Passed\s+Review/)
  assert.match(await reviewRows.nth(1).innerText(), /^Rejected\s+Security review/)
  const summary = reviewRows.first().locator('p')
  assert.equal(await summary.evaluate((el) => getComputedStyle(el).webkitLineClamp), '2',
    'a review summary is clamped to two lines')
  assert.equal((await summary.innerText()).includes('One nit'), false, 'the worker prose is behind the toggle')
  await reviewRows.first().getByRole('button', { name: 'More' }).click()
  await waitForText(page, 'One nit')
  assert.equal(await summary.evaluate((el) => getComputedStyle(el).webkitLineClamp), 'none')
  await reviewRows.first().getByRole('button', { name: 'Less' }).click()
  assert.equal(await summary.evaluate((el) => getComputedStyle(el).webkitLineClamp), '2')

  // An issue whose worker wrote pages of detail still opens with one headline
  // sentence and a short description, above the decisions; the paths, commands
  // and environment variables it wrote stay in Details below them.
  await scenario('summarised')
  await page.goto(base + '/issues/1%3A620')
  await waitForText(page, 'Stop the release job leaking install tokens')
  const headline = page.locator('#issue-headline')
  assert.equal((await headline.innerText()).trim(), 'Stopped: the release job fails before it publishes')
  const plain = (await page.locator('#issue-plain').innerText()).trim()
  assert.equal(plain.split(/(?<=[.!?])\s+/).filter(Boolean).length, 3, 'at most three sentences of description')
  assert.equal(plain.includes('/'), false, 'no paths in the description')

  const summaryBox = await page.locator('#issue-plain').boundingBox()
  const actionsBox = await page.locator('#issue-actions').boundingBox()
  assert.ok(summaryBox.y + summaryBox.height <= actionsBox.y, 'the summary must sit above the action bar')

  await waitForText(page, 'ENEEDAUTH')
  assert.match(await page.locator('#issue-explanation').innerText(), /pipeline\.ts/)
  const detailsBox = await page.locator('#issue-details').boundingBox()
  assert.ok(actionsBox.y + actionsBox.height <= detailsBox.y, 'the worker’s full text sits below the action bar')

  // A pass that still needs a person is a warning, and says why instead of "PASS.".
  const verdicts = page.locator('#issue-reviews > div')
  assert.match(await verdicts.first().innerText(), /^Passed · needs a person\s+Review/)
  assert.match(await verdicts.first().innerText(), /It changes how release credentials are handled\./)
  assert.equal((await verdicts.first().innerText()).includes('PASS.'), false)
  assert.match(await verdicts.nth(1).innerText(), /^Could not review\s+Security review/)

  // Three failed attempts out of three, under the status badge.
  assert.equal((await page.locator('#issue-attempts').innerText()).trim(), '3 of 3 attempts failed')
  // …and the label that paused it, with who applied it and when.
  assert.equal((await page.locator('#issue-held-by').innerText()).trim(), 'Labelled forge:hold by @maintainer on Sep 21')

  // Each pull request is named for what it is to the issue.
  const pulls = page.locator('#issue-actions')
  assert.equal(await pulls.getByRole('link', { name: 'PR #30' }).count(), 1)
  assert.equal(await pulls.getByRole('link', { name: 'Earlier Forge draft #26' }).getAttribute('href'),
    'https://github.com/XRPL-Commons/academy/pull/26')
  assert.equal(await pulls.getByRole('link', { name: 'Other PR #25' }).count(), 1)

  // A queue decision needs no typing, and records who asked for it.
  await scenario('decision-queue')
  await page.goto(base + '/issues/1%3A609')
  await waitForText(page, 'Improve the course completion email')
  await page.getByRole('button', { name: 'Queue for Forge' }).click()
  await waitForText(page, 'Queue for Forge?')
  await waitForText(page, 'Forge will check repository setup, dependencies and existing work before starting when capacity is available.')
  const confirmQueue = page.getByRole('dialog').getByRole('button', { name: 'Queue for Forge' })
  assert.equal(await confirmQueue.isDisabled(), false, 'a reversible decision must not demand a reason')
  await confirmQueue.click()
  await waitForText(page, 'Waiting for a builder')
  assert.equal(await page.getByRole('button', { name: 'Pause', exact: true }).count(), 1)
  assert.equal((await page.locator('body').innerText()).includes('Applying your decision'), false)
  const queued = (await requests()).filter((entry) => entry.method === 'POST' && entry.path === '/api/issues/action')
  assert.equal(queued.length, 1)
  assert.deepEqual(queued[0].body, {
    key: '1:609', action: 'queue', revision: 'current', reason: 'Queue for Forge confirmed by operator',
  })

  // A hold is a decision the operator has to explain.
  await scenario('decision-pause')
  await page.goto(base + '/issues/1%3A608')
  await waitForText(page, 'Keep learners signed in')
  // The API cut this body short, and the issue says so beside it.
  await page.getByRole('button', { name: 'Description' }).click()
  await waitForText(page, 'Description shortened. Open on GitHub to read the full issue.')
  await page.getByRole('button', { name: 'Pause' }).click()
  await waitForText(page, 'Pause?')
  const confirmPause = page.getByRole('dialog').getByRole('button', { name: 'Pause' })
  assert.equal(await confirmPause.isDisabled(), true, 'a hold must be explained before it is applied')
  await page.getByRole('textbox', { name: 'What changed?' }).fill('Waiting for the design review')
  assert.equal(await confirmPause.isDisabled(), false)
  await confirmPause.click()
  await waitForText(page, 'Paused by an operator')
  assert.equal((await page.locator('body').innerText()).includes('Waiting for confirmation'), false)
  const paused = (await requests()).filter((entry) => entry.method === 'POST' && entry.path === '/api/issues/action')
  assert.equal(paused.length, 1)
  assert.deepEqual(paused[0].body, {
    key: '1:608', action: 'pause', revision: 'current', reason: 'Waiting for the design review',
  })

  // An issue waiting for a human approval offers the one decision that helps:
  // asking named people for the review. The button opens a dialog whose select
  // starts from the configured human reviewers and offers no free text;
  // confirming sends the chosen logins as the decision.
  await scenario('approval')
  await page.goto(base + '/issues/1%3A614')
  await waitForText(page, 'Rotate the release signing key')
  await waitForText(page, 'The pull request needs a human review.')
  const reviewerBar = page.locator('#issue-actions')
  assert.equal(await reviewerBar.getByRole('button', { name: 'Reviewers' }).count(), 0, 'the bar holds one button, not a select')
  assert.equal(await reviewerBar.getByRole('textbox').count(), 0)
  await reviewerBar.getByRole('button', { name: 'Assign reviewer' }).click()
  const reviewerDialog = page.getByRole('dialog')
  await reviewerDialog.waitFor()
  await waitForText(page, 'Forge asks GitHub to request a review of PR #31 from the people you choose')
  assert.equal(await reviewerDialog.getByRole('textbox').count(), 0, 'no login can be typed')
  const reviewers = reviewerDialog.getByRole('button', { name: 'Reviewers' })
  assert.match(await reviewers.innerText(), /maintainer/, 'the configured human reviewers start selected')
  await reviewers.click()
  await waitForText(page, 'recently merged work here')
  await page.getByRole('option', { name: /second-pair/ }).click()
  await page.keyboard.press('Escape')
  await reviewerDialog.getByRole('button', { name: 'Assign reviewer' }).click()
  await waitForText(page, 'Decision submitted. Waiting for confirmation.')
  assert.equal(await page.getByRole('dialog').count(), 0, 'the dialog closes once Forge accepts the decision')
  const assigned = (await requests()).filter((entry) => entry.method === 'POST' && entry.path === '/api/issues/action')
  assert.equal(assigned.length, 1)
  assert.deepEqual(assigned[0].body, {
    key: '1:614', action: 'assign_reviewer', revision: 'current', reason: 'maintainer, second-pair',
  })

  // Once recorded, the issue says who was asked. With no known reviewers (a
  // failed listing), the dialog says so and points at the pull request on
  // GitHub — there is nothing to type and nothing to submit.
  await scenario('approval-requested')
  await page.goto(base + '/issues/1%3A614')
  await waitForText(page, 'Review requested from @maintainer')
  assert.match((await page.locator('#review-requested').innerText()).trim(), /^Review requested from @maintainer( · .+ ago)?$/)
  await page.locator('#issue-actions').getByRole('button', { name: 'Assign reviewer' }).click()
  const noneDialog = page.getByRole('dialog')
  await noneDialog.waitFor()
  await waitForText(page, 'Forge could not find any known reviewers for this repository.')
  assert.equal(await noneDialog.getByRole('textbox').count(), 0, 'no free-text fallback')
  assert.equal(await noneDialog.getByRole('button', { name: 'Assign reviewer' }).count(), 0, 'nothing to submit')
  assert.equal(await noneDialog.getByRole('link', { name: 'Open PR #31 on GitHub ↗' }).getAttribute('href'),
    'https://github.com/XRPL-Commons/academy/pull/31')
  await noneDialog.getByRole('button', { name: 'Close' }).click()
  await page.getByRole('dialog').waitFor({ state: 'detached' })
  assert.equal((await requests()).filter((entry) => entry.method === 'POST' && entry.path === '/api/issues/action').length, 0)

  // No other issue offers it: the decision belongs to a waiting pull request.
  await scenario('decision-queue')
  await page.goto(base + '/issues/1%3A609')
  await waitForText(page, 'Improve the course completion email')
  assert.equal(await page.locator('#issue-actions').getByRole('button', { name: 'Assign reviewer' }).count(), 0)
  assert.equal(await page.locator('#review-requested').count(), 0)

  // Stopping an attempt targets the job, so it goes to the factory.
  await scenario('default')
  await page.goto(base + '/issues/1%3A610')
  await waitForText(page, DETAIL_TITLE)
  await page.getByRole('button', { name: 'Stop attempt' }).click()
  await waitForText(page, 'Stop attempt?')
  const confirmStop = page.getByRole('dialog').getByRole('button', { name: 'Stop attempt' })
  assert.equal(await confirmStop.isDisabled(), true)
  await page.getByRole('textbox', { name: 'What changed?' }).fill('Stuck for an hour')
  await confirmStop.click()
  await waitForText(page, 'Decision submitted. Waiting for confirmation.')
  const stopped = (await requests()).filter((entry) => entry.method === 'POST' && entry.path === '/api/factory/action')
  assert.equal(stopped.length, 1)
  assert.deepEqual(stopped[0].body, { action: 'stop', target: 'attempt', reason: 'Stuck for an hour' })
  assert.equal((await requests()).filter((entry) => entry.path === '/api/issues/action').length, 0)

  // Refreshing asks GitHub for a fresh inventory.
  await scenario('default')
  await page.goto(base + '/issues')
  await waitForText(page, 'Redact credential values at the logging boundary')
  await page.getByRole('button', { name: 'Refresh issues' }).click()
  const refreshed = await waitForRequest(requests, (entry) => entry.method === 'POST' && entry.path === '/api/issues/refresh')
  assert.equal(refreshed.length, 1)
  assert.deepEqual(refreshed[0].body, {})

  // A decision the supervisor has not applied yet is visible from both the
  // list and the issue, without either offering the decision again.
  await scenario('decision-pending')
  await page.goto(base + '/issues')
  await waitForText(page, 'Redact credential values at the logging boundary')
  const pendingRow = page.locator('[data-key="1:610"]')
  await pendingRow.getByRole('link', { name: 'Applying…' }).waitFor({ timeout: 10000 })
  assert.equal(await pendingRow.getByRole('link', { name: 'Applying…' }).count(), 1)
  await page.goto(base + '/issues/1%3A610')
  await waitForText(page, 'Applying your decision…')
  assert.equal((await page.locator('#detail-message').innerText()).trim(), 'Applying your decision…')

  // No repository configured: the list says where to start.
  await scenario('no-repos')
  await page.goto(base + '/issues')
  await waitForText(page, 'Choose where Forge should work')
  await waitForText(page, 'Add and enable a repository to see its issues here.')
  const addRepository = page.getByRole('link', { name: 'Add repository →' })
  assert.equal(await addRepository.getAttribute('href'), '/settings/repository')
  assert.equal(await page.locator('.issue-row').count(), 0)

  // A reset fence replaces the list on the next poll and hides every row.
  await scenario('paused')
  await page.goto(base + '/issues')
  await waitForText(page, 'Factory reset is in progress')
  assert.equal(await page.locator('.issue-row').count(), 0)
  assert.equal(
    await page.evaluate(() => document.body.innerText.split('Factory reset is in progress').length - 1), 1,
    'the fence is stated once',
  )

  // On a phone-width viewport nothing scrolls sideways.
  await scenario('default')
  await page.setViewportSize({ width: 390, height: 844 })
  for (const path of ['/issues', '/issues/1%3A610']) {
    await page.goto(base + path)
    await waitForText(page, path === '/issues' ? 'Redact credential values at the logging boundary' : DETAIL_TITLE)
    const overflow = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth,
    }))
    assert.ok(overflow.scrollWidth <= overflow.innerWidth, `${path} scrolls sideways: ${JSON.stringify(overflow)}`)
    if (path === '/issues') {
      // On a phone the card's button sits below its text, not beside it.
      const facts = await page.locator('[data-key="1:613"] ul').boundingBox()
      const button = await page.locator('[data-key="1:613"]').getByRole('link', { name: 'Recheck readiness' }).boundingBox()
      assert.ok(button.y >= facts.y + facts.height, `the action stacks below the text: ${JSON.stringify({ facts, button })}`)
    }
  }
}
