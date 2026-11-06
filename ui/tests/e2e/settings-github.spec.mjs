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
// setup phase, and its three outcomes), with a link to repository management.
export default async function (page, { base, scenario, requests }) {
  await page.goto(base + '/settings/github')
  await waitForText(page, 'Choose Manage access on GitHub')
  // The original role illustrations survive on both migrated UI surfaces.
  for (const role of ['build', 'review', 'audit', 'controller']) {
    const art = page.locator(`#connections svg.role-illustration[data-role="${role}"]`)
    await art.waitFor()
    assert.equal(await art.getAttribute('aria-hidden'), 'true')
    assert.ok(await art.locator('path, rect, circle').count() > 0)
    assert.equal(await art.locator('[data-github-badge]').count(), 1)
  }
  for (const width of [320, 390, 1440]) {
    await page.setViewportSize({ width, height: 1000 })
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `role artwork overflow at ${width}`)
    await page.screenshot({ path: `/tmp/forge-roles-settings-github-${width}.png`, fullPage: true })
  }


  assert.equal(await page.title(), 'GitHub access · Forge')
  assert.equal(await page.getByRole('link', { name: 'Connections', exact: true }).getAttribute('aria-current'), 'page')

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

  assert.equal(await page.getByRole('button', { name: 'Add repository' }).count(), 0)
  assert.equal(await page.locator('#repo-count').count(), 0)
  assert.equal(await page.getByRole('link', { name: 'Repositories', exact: true }).count(), 2)

  // Refresh on a live factory starts the background access sync, and reports
  // it finished when a newer `finished_at` arrives.
  await scenario('default')
  await page.goto(base + '/settings/github')
  await waitForText(page, 'Choose Manage access on GitHub')
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

  // A refused request is reported with the page's own sentence, in the error
  // tone: it is a failure the operator has to act on, not a progress note.
  await scenario('sync-failed')
  await page.goto(base + '/settings/github')
  await waitForText(page, 'Choose Manage access on GitHub')
  await page.getByRole('button', { name: 'Refresh GitHub access' }).click()
  await waitForText(page, 'Could not refresh access. Try again.')
  const failure = page.locator('#notice')
  assert.equal((await failure.innerText()).trim(), 'Could not refresh access. Try again.')
  assert.equal(await failure.getAttribute('role'), 'alert')
  assert.ok((await failure.getAttribute('class')).includes('text-error'), await failure.getAttribute('class'))
  assert.equal(await page.locator('#refresh-result').count(), 0)

  // While Forge is still being set up: the badges tell credentials from
  // registrations, each role offers exactly one action, and refresh goes to
  // the onboarding endpoint instead of the settings sync.
  await scenario('needs-apps')
  await page.goto(base + '/settings/github')
  await waitForText(page, 'Choose Manage access on GitHub')
  await page.locator('#connections [data-badge]').nth(3).waitFor()
  const badges = await page.locator('#connections [data-badge]').allInnerTexts()
  assert.deepEqual(badges, ['Connected', 'Installation unverified', 'Permissions missing', 'Not connected'])
  assert.ok((await page.locator('#connections [data-key="audit"]').innerText()).includes('Missing contents: write'))
  assert.ok((await page.locator('#connections [data-key="review"]').innerText()).includes('forge-review-example'))
  assert.ok((await page.locator('#connections [data-key="controller"]').innerText()).includes('No App connected'))
  assert.equal(await page.getByRole('link', { name: 'Install App ↗' }).getAttribute('href'), INSTALL_REVIEW)
  assert.equal(await page.getByRole('link', { name: 'Connect in setup →' }).getAttribute('href'), '/onboarding')
  await page.getByRole('button', { name: 'Refresh GitHub access' }).click()
  const refreshes = await waitForRequests(requests, posted('/api/onboarding'))
  assert.deepEqual(refreshes[0].body, { action: 'refresh' })
  assert.equal((await requests()).filter(posted('/api/settings/sync')).length, 0)

  // A migrated installation shares one App across all four roles. Exercise
  // the real page with the live API shape while keeping the legacy fixtures.
  await scenario('default')
  const settingsRoute = '**/api/settings?summary=1'
  await page.route(settingsRoute, async route => {
    const response = await route.fetch()
    const settings = await response.json()
    for (const role of ['build', 'review', 'audit', 'controller']) {
      settings.config.connections[role] = { ...settings.config.connections.controller }
      settings.roles[role] = { ...settings.roles[role], app_slug: 'forge-shared-example',
        installation_url: 'https://github.com/organizations/XRPL-Commons/settings/installations/104' }
    }
    await route.fulfill({ response, json: settings })
  })
  await page.goto(base + '/settings/github')
  await page.waitForFunction(() => document.querySelectorAll('#connections [data-key]').length === 1)
  const shared = page.locator('#connections [data-key]')
  const sharedText = await shared.innerText()
  for (const text of ['Forge App', 'forge-shared-example', 'Connected', '2 repositories with verified access']) {
    assert.ok(sharedText.includes(text), `shared App card is missing ${text}: ${sharedText}`)
  }
  assert.equal(await shared.getByRole('link', { name: 'Manage access ↗' }).getAttribute('href'),
    'https://github.com/organizations/XRPL-Commons/settings/installations/104')
  assert.equal(await page.getByText('See the four Forge Apps', { exact: false }).count(), 0)
  await page.screenshot({ path: '/tmp/forge-single-app-settings-github.png', fullPage: true })
  await page.unroute(settingsRoute)

  // On a phone-width viewport nothing scrolls sideways.
  await scenario('default')
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(base + '/settings/github')
  await waitForText(page, 'Choose Manage access on GitHub')
  const overflow = await page.evaluate(() => ({ scrollWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth }))
  assert.ok(overflow.scrollWidth <= overflow.innerWidth, `page scrolls sideways: ${JSON.stringify(overflow)}`)
}
