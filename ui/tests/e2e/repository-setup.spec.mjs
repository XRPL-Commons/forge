import assert from 'node:assert/strict'

const RULESET = '/api/repositories/ruleset?repo_id=2&revision=settings-rev-1'

function waitForText(page, text) {
  return page.waitForFunction((needle) => document.body.innerText.includes(needle), text, { timeout: 10000 })
}

async function waitForRequests(requests, match, timeout = 10000) {
  const deadline = Date.now() + timeout
  for (;;) {
    const found = (await requests()).filter(match)
    if (found.length) return found
    if (Date.now() > deadline) throw new Error('timed out waiting for a request: ' + match.toString())
    await new Promise((resolve) => setTimeout(resolve, 50))
  }
}

const posted = (path) => (entry) => entry.method === 'POST' && entry.path === path

// The step the strip marks as current, e.g. "Enable".
function currentStep(page) {
  return page.locator('#repository-stage [aria-current="step"] [data-label]').innerText()
}

// End-to-end behaviour of Settings · Repository setup: the picker, the four
// setup steps, the review-rules download (filename and Content-Disposition
// through the Nitro proxy), enabling automation, and the automatic re-check.
export default async function (page, { base, scenario, requests }) {
  // Picker: only repositories that are not enabled yet, each linking to its
  // own setup page; the strip is on step one.
  await page.goto(base + '/settings/repository')
  await waitForText(page, 'Choose your repository')
  assert.equal(await page.title(), 'Prepare repository · Forge')
  assert.equal((await page.getByRole('heading', { level: 1 }).innerText()).trim(), 'Add repository')
  assert.equal((await currentStep(page)).trim(), 'Repository')

  const options = page.locator('#repo-options li')
  assert.equal(await options.count(), 1)
  assert.equal(await options.first().getByRole('link').getAttribute('href'), '/settings/repository?repo_id=2')

  await page.getByRole('searchbox', { name: 'Find by name' }).fill('academy')
  await waitForText(page, 'No matching repositories yet.')
  await page.getByRole('searchbox', { name: 'Find by name' }).fill('')
  await waitForText(page, 'XRPL-Commons/hackathon')

  await page.getByRole('button', { name: 'Repository missing?' }).click()
  await waitForText(page, 'Give a Forge App access to the repository on GitHub using the links below, then refresh access.')

  // Choosing a repository moves the page to ?repo_id= and to the last step:
  // access and rules are in place, only enabling is left.
  await page.locator('#repo-options').getByRole('link', { name: 'XRPL-Commons/hackathon' }).click()
  await page.waitForURL(base + '/settings/repository?repo_id=2')
  await waitForText(page, 'Enable repository')
  assert.equal((await currentStep(page)).trim(), 'Enable')
  assert.equal((await page.locator('#status').innerText()).trim(), 'Ready to enable')
  assert.equal(
    (await page.locator('#description').innerText()).trim(),
    'Setup is complete. Enable automation to let Forge pick up eligible issues in this repository.',
  )
  assert.ok((await page.locator('#checked-at').innerText()).startsWith('Last checked '))
  // App access is complete, so the panel is not in the way.
  assert.equal(await page.locator('#app-access').count(), 0)

  // Enable posts the repository and revision, and the returned view replaces
  // the snapshot: the pill reads Enabled without waiting for a poll.
  await page.getByRole('button', { name: 'Enable automation' }).click()
  const enables = await waitForRequests(requests, posted('/api/repositories/enable'))
  assert.equal(enables.length, 1)
  assert.deepEqual(enables[0].body, { repo_id: 2, revision: 'settings-rev-1' })
  await page.waitForFunction(() => document.getElementById('status').innerText.trim() === 'Enabled', null, { timeout: 10000 })
  assert.equal((await page.getByRole('heading', { level: 1 }).innerText()).trim(), 'Repository setup')
  assert.equal(
    (await page.locator('#description').innerText()).trim(),
    'Setup is complete and automation is enabled. No action is needed here.',
  )

  // A repository whose review rules are missing: the failed requirements, the
  // import steps, and the ruleset download.
  await scenario('needs-setup')
  await page.goto(base + '/settings/repository?repo_id=2')
  await waitForText(page, 'Update the GitHub review rules below, then check again.')
  assert.equal((await currentStep(page)).trim(), 'Review rules')
  assert.equal((await page.locator('#status').innerText()).trim(), 'Setup needed')

  const failed = await page.locator('#requirements li').allInnerTexts()
  assert.equal(failed.length, 2)
  assert.ok(failed[0].includes('At least two approving reviews'), failed[0])
  assert.ok(failed[0].includes('Require pull requests with at least two approving reviews.'), failed[0])
  assert.ok(failed[1].includes('Fresh approvals when code changes'), failed[1])
  assert.ok(failed[1].includes('Dismiss stale pull request approvals when new commits are pushed.'), failed[1])

  await waitForText(page, 'On GitHub, choose New ruleset → Import a ruleset. Select the file, review the settings and save.')
  assert.equal(
    (await page.locator('#scope').innerText()).trim(),
    'Applies to every pull request targeting main. Existing organization rules and stronger protections still apply.',
  )
  assert.equal(await page.locator('#github').getAttribute('href'), 'https://github.com/XRPL-Commons/hackathon/settings/rules')

  const download = page.locator('#download')
  assert.equal(await download.getAttribute('download'), 'forge-review-rules-2.json')
  assert.equal(await download.getAttribute('href'), RULESET)

  // The generated file keeps its name end to end: the Nitro proxy passes the
  // upstream Content-Disposition through, and the browser saves it under it.
  const header = await page.evaluate((url) => fetch(url).then((response) => response.headers.get('content-disposition')), RULESET)
  assert.equal(header, 'attachment; filename="forge-review-rules-2.json"')
  const [saved] = await Promise.all([page.waitForEvent('download'), download.click()])
  assert.equal(saved.suggestedFilename(), 'forge-review-rules-2.json')
  await waitForText(page, 'Download again')

  // "Refresh repository" asks for a fresh protection check of this repository.
  await page.getByRole('button', { name: 'Refresh repository' }).click()
  const checks = await waitForRequests(requests, posted('/api/repositories/check'))
  assert.equal(checks.length, 1)
  assert.deepEqual(checks[0].body, { repo_id: 2, revision: 'settings-rev-1' })

  // The bypass caveat is one collapsible away, never hidden from the operator.
  await page.getByRole('button', { name: 'Setup details' }).click()
  await waitForText(page, 'Bypass permissions need your review.')
  await waitForText(page, 'GitHub may hide who can bypass a ruleset. Check the bypass list on GitHub; a successful check here does not certify that list.')
  await waitForText(page, 'Forge reads review requirements using its existing GitHub access. It cannot change your rules.')

  // A stale report is re-checked without operator action, exactly once per
  // observation.
  // The previous page is parked first: switching the scenario under a live
  // page would let that page observe the stale row as well, and this
  // assertion is about one page's behaviour.
  await page.goto('about:blank')
  await scenario('stale')
  // Counted from the browser, not from the mock's shared request log, so the
  // "exactly once" claim cannot be confused by anything else on the harness.
  const autoChecks = []
  const onRequest = (request) => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/repositories/check') autoChecks.push(request.postDataJSON())
  }
  page.on('request', onRequest)
  await page.goto(base + '/settings/repository?repo_id=2')
  await waitForText(page, 'Check again to confirm the current GitHub settings before continuing.')
  for (const deadline = Date.now() + 10000; !autoChecks.length && Date.now() < deadline;) await new Promise((resolve) => setTimeout(resolve, 50))
  assert.equal(autoChecks.length, 1, 'the page must check a stale report without operator action')
  assert.deepEqual(autoChecks[0], { repo_id: 2, revision: 'settings-rev-1' })
  // A poll cycle later (this page polls every 3 s) it has not asked again.
  await page.waitForTimeout(3500)
  assert.equal(autoChecks.length, 1, 'the auto-check must fire once per observation')
  page.off('request', onRequest)

  // On a phone-width viewport nothing scrolls sideways.
  await scenario('needs-setup')
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(base + '/settings/repository?repo_id=2')
  await waitForText(page, 'Setup needed')
  const overflow = await page.evaluate(() => ({ scrollWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth }))
  assert.ok(overflow.scrollWidth <= overflow.innerWidth, `page scrolls sideways: ${JSON.stringify(overflow)}`)
}
