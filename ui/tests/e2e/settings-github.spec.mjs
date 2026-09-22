import assert from 'node:assert/strict'

const MANAGE_BUILD = 'https://github.com/organizations/XRPL-Commons/settings/installations/101'
const INSTALL_REVIEW = 'https://github.com/apps/forge-review-example/installations/new'

function waitForText(page, text) {
  return page.waitForFunction((needle) => document.body.innerText.includes(needle), text, { timeout: 10000 })
}

// Waits until the mock has recorded a request matching `match`, and returns
// every matching entry (the request log is cleared by `scenario()`).
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

// End-to-end behaviour of Settings · GitHub access: one card per Forge App
// with its badge and its single action link, the access refresh (endpoint by
// setup phase, and its three outcomes), and the repository table.
export default async function (page, { base, scenario, requests }) {
  await page.goto(base + '/settings/github')
  await waitForText(page, 'See the four Forge Apps')

  assert.equal(await page.title(), 'GitHub access · Forge')
  assert.equal(await page.getByRole('link', { name: 'GitHub access' }).getAttribute('aria-current'), 'page')

  // One card per role, in role order, each with its identity, purpose, badge,
  // observation and action link.
  const cards = page.locator('#connections [data-key]')
  assert.equal(await cards.count(), 4)
  assert.deepEqual(await cards.evaluateAll((nodes) => nodes.map((node) => node.dataset.key)), ['build', 'review', 'audit', 'controller'])

  const build = page.locator('#connections [data-key="build"]')
  const buildText = await build.innerText()
  for (const expected of ['Builder', 'forge-build-xrpl-commons', 'Writes code and opens pull requests.', 'Connected', '2 repositories with verified access']) {
    assert.ok(buildText.includes(expected), `builder card is missing ${expected}: ${buildText}`)
  }
  assert.equal(await build.getByRole('link', { name: 'Manage access ↗' }).getAttribute('href'), MANAGE_BUILD)
  assert.ok((await page.locator('#connections [data-key="controller"]').innerText()).includes('Coordinates issues and merges approved pull requests.'))
  assert.ok((await page.locator('#sync-status').innerText()).startsWith('Last refresh '))

  // The repositories table: state icon, name link, and the action that suits
  // each row. Enabling never happens from this table.
  assert.equal((await page.locator('#repo-count').innerText()).trim(), '1 enabled · 2 discovered or configured')
  const enabled = page.locator('[data-key="1"]')
  assert.equal(await enabled.getByRole('img', { name: 'Enabled' }).count(), 1)
  assert.equal(await enabled.getByRole('link', { name: 'XRPL-Commons/academy' }).getAttribute('href'), '/settings/repository?repo_id=1')
  assert.equal(await page.locator('[data-key="2"]').getByRole('img', { name: 'Not enabled' }).count(), 1)
  assert.equal(await page.getByRole('link', { name: 'Set up' }).getAttribute('href'), '/settings/repository?repo_id=2')
  assert.equal(await page.getByRole('link', { name: 'Add repository' }).getAttribute('href'), '/settings/repository')

  // The search filters the rendered rows client-side.
  await page.getByRole('searchbox', { name: 'Find a repository' }).fill('hack')
  await page.waitForFunction(() => document.querySelectorAll('tbody tr').length === 1, null, { timeout: 5000 })
  assert.equal(await page.locator('[data-key="2"]').count(), 1)
  await page.getByRole('searchbox', { name: 'Find a repository' }).fill('')
  await page.waitForFunction(() => document.querySelectorAll('tbody tr').length === 2, null, { timeout: 5000 })

  // "Refresh repositories" asks for a protection check of every enabled repo.
  await page.getByRole('button', { name: 'Refresh repositories' }).click()
  const checks = await waitForRequests(requests, posted('/api/repositories/check'))
  assert.equal(checks.length, 1)
  assert.deepEqual(checks[0].body, { repo_id: null, revision: 'settings-rev-1' })

  // Disable writes the settings back with that repository switched off.
  await page.getByRole('button', { name: 'Disable' }).click()
  const saves = await waitForRequests(requests, posted('/api/settings'))
  assert.equal(saves.length, 1)
  assert.equal(saves[0].body.revision, 'settings-rev-1')
  assert.deepEqual(saves[0].body.config.repositories, [
    { id: 1, name: 'XRPL-Commons/academy', enabled: false },
    { id: 2, name: 'XRPL-Commons/hackathon', enabled: false },
  ])
  await waitForText(page, 'Settings saved.')

  // Refresh on a live factory starts the background access sync, and reports
  // it finished when a newer `finished_at` arrives.
  await scenario('default')
  await page.goto(base + '/settings/github')
  await waitForText(page, 'See the four Forge Apps')
  await page.getByRole('button', { name: 'Refresh GitHub access' }).click()
  const syncs = await waitForRequests(requests, posted('/api/settings/sync'))
  assert.deepEqual(syncs[0].body, {})
  assert.equal((await requests()).filter(posted('/api/onboarding')).length, 0)
  await waitForText(page, 'Checking App access…')
  await scenario('refreshed')
  await waitForText(page, 'Access refreshed.')

  // A sync the supervisor gives up on is reported, and the status line says
  // the shown data is the previous one.
  await page.getByRole('button', { name: 'Refresh GitHub access' }).click()
  await waitForText(page, 'Checking App access…')
  await scenario('interrupted')
  await waitForText(page, 'The access check did not finish. Refresh access to try again.')
  await page.waitForFunction(
    () => document.getElementById('sync-status').innerText.includes('· Refresh interrupted; the previous data is shown.'),
    null, { timeout: 10000 },
  )

  // A refused request is reported with the page's own sentence.
  await scenario('sync-failed')
  await page.goto(base + '/settings/github')
  await waitForText(page, 'See the four Forge Apps')
  await page.getByRole('button', { name: 'Refresh GitHub access' }).click()
  await waitForText(page, 'Could not refresh access. Try again.')

  // While Forge is still being set up: the badges tell credentials from
  // registrations, each role offers exactly one action, and refresh goes to
  // the onboarding endpoint instead of the settings sync.
  await scenario('needs-apps')
  await page.goto(base + '/settings/github')
  await waitForText(page, 'See the four Forge Apps')
  const badges = await page.locator('#connections [data-badge]').allInnerTexts()
  assert.deepEqual(badges, ['Connected', 'Installation unverified', 'Permissions missing', 'Not connected'])
  assert.ok((await page.locator('#connections [data-key="audit"]').innerText()).includes('Missing contents: write'))
  assert.ok((await page.locator('#connections [data-key="review"]').innerText()).includes('forge-review-example'))
  assert.ok((await page.locator('#connections [data-key="controller"]').innerText()).includes('No App connected'))
  assert.equal(await page.getByRole('link', { name: 'Install App ↗' }).getAttribute('href'), INSTALL_REVIEW)
  assert.equal(await page.getByRole('link', { name: 'Connect in setup →' }).getAttribute('href'), '/onboarding')
  assert.equal((await page.locator('#repo-count').innerText()).trim(), 'Setup in progress · 2 discovered or configured')
  assert.equal(await page.getByRole('button', { name: 'Refresh repositories' }).count(), 0)
  assert.equal(await page.getByRole('link', { name: 'Continue setup →' }).first().getAttribute('href'), '/onboarding')

  await page.getByRole('button', { name: 'Refresh GitHub access' }).click()
  const refreshes = await waitForRequests(requests, posted('/api/onboarding'))
  assert.deepEqual(refreshes[0].body, { action: 'refresh' })
  assert.equal((await requests()).filter(posted('/api/settings/sync')).length, 0)

  // On a phone-width viewport nothing scrolls sideways.
  await scenario('default')
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(base + '/settings/github')
  await waitForText(page, 'XRPL-Commons/academy')
  const overflow = await page.evaluate(() => ({ scrollWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth }))
  assert.ok(overflow.scrollWidth <= overflow.innerWidth, `page scrolls sideways: ${JSON.stringify(overflow)}`)
}
