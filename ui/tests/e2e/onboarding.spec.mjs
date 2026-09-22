import assert from 'node:assert/strict'

const MOCK_BASE = 'http://127.0.0.1:8788'
const LOGIN_COOKIE = /(^|;\s*)forge_setup=[A-Za-z0-9_-]{32,128}(;|$)/

// textContent, not innerText: the eyebrows are styled uppercase, and these
// assertions are about the copy, not about the CSS.
function waitForText(page, text) {
  return page.waitForFunction((needle) => document.body.textContent.includes(needle), text, { timeout: 10000 })
}

function text(page) {
  return page.evaluate(() => document.body.textContent)
}

function posts(entries, path = '/api/onboarding') {
  return entries.filter((entry) => entry.method === 'POST' && entry.path === path)
}

// The mock records requests server side; poll it from node rather than from
// the page, which cannot read the mock's origin.
async function waitForPost(requests, match, what) {
  const deadline = Date.now() + 10000
  for (;;) {
    const found = posts(await requests()).find(match)
    if (found) return found
    if (Date.now() > deadline) throw new Error(`timed out waiting for the ${what} POST`)
    await new Promise((resolve) => setTimeout(resolve, 100))
  }
}

// Moves the router without loading a document, the way clicking a NuxtLink
// does. `marker` survives only if no navigation happened.
async function clientNavigate(page, to) {
  await page.evaluate(() => { window.__clientNav = true })
  const pushed = await page.evaluate((target) => {
    const root = document.querySelector('#__nuxt') ?? document.body.firstElementChild
    const app = root && root.__vue_app__
    if (!app) return false
    app.config.globalProperties.$router.push(target)
    return true
  }, to)
  assert.ok(pushed, 'could not reach the Vue router for a client-side navigation')
  await page.waitForFunction((target) => location.pathname === target, to, { timeout: 10000 })
  assert.equal(await page.evaluate(() => window.__clientNav === true), true, `navigating to ${to} reloaded the document`)
}

// End-to-end behaviour of guided setup: the three steps and what each one
// sends, the reset screen, the redirects that keep an operator inside setup,
// and the forge_setup cookie reaching Python after a client-side navigation.
export default async function (page, { base, scenario, requests }) {
  // --- Step 1: owner, account type, and the App-manifest hand-off --------
  await scenario('setup-fresh')
  await page.goto(base + '/onboarding')
  assert.equal(await page.title(), 'Setup · Forge')
  await waitForText(page, 'Connect your GitHub workspace')

  const step1 = await text(page)
  assert.ok(step1.includes('1 · GitHub'), step1)
  assert.ok(step1.includes('You own the Apps and choose which repositories they can access.'))
  for (const name of ['Builder', 'Reviewer', 'Auditor', 'Controller']) assert.ok(step1.includes(name), name)
  assert.ok(step1.includes('Create a distinct GitHub App'))
  assert.ok(step1.includes('GitHub will ask you to approve each App and its installation.'))
  // Nothing is registered yet, so the rail cannot skip ahead.
  assert.equal(await page.locator('[data-step="1"]').isDisabled(), true)
  assert.equal(await page.locator('[data-step="2"]').isDisabled(), true)

  await page.locator('#owner').fill('acme-labs')
  await page.locator('#account-type').click()
  await page.getByRole('option', { name: 'Personal account' }).click()
  await page.getByRole('button', { name: 'Connect Builder' }).click()

  // The manifest is handed over by a form POST, and the mock answers 303.
  await page.waitForURL(MOCK_BASE + '/onboarding', { timeout: 10000 })
  const connected = await requests()
  assert.deepEqual(posts(connected)[0].body, {
    action: 'github', role: 'build', owner: 'acme-labs', account_type: 'personal',
  })
  const manifest = posts(connected, '/__manifest')
  assert.equal(manifest.length, 1, 'the manifest form was not submitted to the returned action')
  assert.deepEqual(JSON.parse(manifest[0].body.manifest), { name: 'forge-build' })

  // --- Step 1 with a known owner: role copy, and the boot refresh --------
  await scenario('setup')
  await page.goto(base + '/onboarding')
  await waitForText(page, 'GitHub owner: example')
  const known = await text(page)
  assert.ok(known.includes('Connected'), known)
  assert.ok(known.includes('App created · install it on GitHub'))
  assert.equal(await page.locator('#owner').count(), 0, 'a known owner is stated, not asked for again')
  assert.equal(await page.getByRole('button', { name: 'Install Auditor' }).count(), 1)
  // Contracts §3.8 boot rule: some role registered, nothing selected, no
  // operation -> the page discovers installations by itself.
  await waitForPost(requests, (entry) => entry.body?.action === 'refresh', 'boot refresh')

  // --- Step 3: verify the repository ------------------------------------
  await scenario('setup-step3')
  await page.goto(base + '/onboarding')
  await waitForText(page, 'Verify your repository')
  const step3 = await text(page)
  assert.ok(step3.includes('3 · Start'), step3)
  assert.ok(step3.includes('Your coding accounts are connected. Repository verification is still required before starting.'))
  assert.ok(step3.includes('XRPL-Commons/academy'))
  assert.ok(step3.includes('Eligible issues labeled ready · Build → Review → Audit → Merge'))
  assert.ok(step3.includes('Verifies accounts and worker isolation. This includes small model requests that may incur provider charges.'))
  // The coding-accounts summary, from the same payload.
  assert.ok(step3.includes('Coding accounts'))
  assert.ok(step3.includes('Not connected'), 'Grok is not connected in this fixture')

  await page.getByRole('button', { name: 'Technical checks' }).click()
  await waitForText(page, 'Worker stop, cleanup and reuse')
  const checks = await text(page)
  assert.ok(checks.includes('Repository permissions and protection'), checks)
  assert.ok(checks.includes('Coding account connections and isolation'))
  assert.ok(checks.includes('Not checked'))

  await page.getByRole('button', { name: 'How Forge will work' }).click()
  await waitForText(page, 'Build → Review → Security audit → Merge.')

  await page.getByRole('button', { name: 'Verify repository' }).click()
  const checked = await waitForPost(requests, (entry) => entry.body?.action === 'check', 'check')
  assert.deepEqual(checked.body, { action: 'check', revision: 'rev-2' })
  assert.match(checked.cookie ?? '', LOGIN_COOKIE)

  // --- Step 2: choosing the repository ----------------------------------
  await page.locator('[data-step="1"]').click()
  await waitForText(page, 'Where should Forge work?')
  const step2 = await text(page)
  assert.ok(step2.includes('Choose one repository to start with. You can add more later.'), step2)
  assert.ok(step2.includes('All four roles have access'))
  assert.ok(step2.includes('Selecting a repository does not start work.'))
  const choices = page.getByRole('radio')
  assert.equal(await choices.count(), 1, 'the fixture offers exactly one shared repository')
  assert.equal(await choices.first().getAttribute('aria-checked'), 'true', 'the selected repository is preselected')
  await choices.first().click()
  await page.getByRole('button', { name: 'Continue' }).click()
  await waitForText(page, '3 · Start')
  const selected = await waitForPost(requests, (entry) => entry.body?.action === 'repository', 'repository')
  assert.deepEqual(selected.body, { action: 'repository', repo_id: 1 })

  // --- Reset in progress -------------------------------------------------
  await scenario('resetting')
  await page.goto(base + '/onboarding')
  await waitForText(page, 'Resetting Forge')
  const resetting = await text(page)
  assert.ok(resetting.includes('Reset in progress'), resetting)
  assert.ok(resetting.includes('Stopping workers and verifying cleanup'), 'the operation step is named')
  assert.ok(resetting.includes('New work is blocked while Forge stops and cleans up its resources.'))
  assert.ok(resetting.includes('Reset continues if you close this page. Fresh setup becomes available after cleanup is verified.'))
  assert.equal(await page.getByRole('link', { name: 'Reset Forge' }).count(), 0, 'the reset entry is hidden while resetting')

  // Every other route is fenced back to /onboarding while resetting.
  await page.goto(base + '/issues')
  await page.waitForURL(base + '/onboarding', { timeout: 10000 })
  await waitForText(page, 'Resetting Forge')

  // --- / redirects into setup, with an unavailable factory ---------------
  await scenario('setup')
  await page.goto(base + '/')
  await page.waitForURL(base + '/onboarding', { timeout: 10000 })
  await waitForText(page, 'Connect your GitHub workspace')
  assert.equal(await page.getByRole('link', { name: 'Forge home' }).count(), 1, 'the header survives {ok:false} on /api/factory')
  await page.goto(base + '/settings')
  await page.waitForURL(base + '/onboarding', { timeout: 10000 })

  // --- The setup cookie survives a client-side navigation ----------------
  // E3: the recorded `cookie` proves Python receives it, not merely that
  // Nitro set it. Cookies are cleared after the document load, so only the
  // page's own GET /_ui/setup-cookie can put one back.
  await scenario('setup-fresh')
  await page.goto(base + '/settings/harnesses')
  await waitForText(page, 'Manage sign-in from the Forge VM terminal.')
  await page.context().clearCookies()
  assert.equal((await page.context().cookies(base)).length, 0)

  await clientNavigate(page, '/onboarding')
  await waitForText(page, 'Connect your GitHub workspace')
  const cookie = (await page.context().cookies(base)).find((entry) => entry.name === 'forge_setup')
  assert.ok(cookie, 'the page did not restore the forge_setup cookie on mount')
  assert.match(cookie.value, /^[A-Za-z0-9_-]{32,128}$/)
  assert.equal(cookie.httpOnly, true)
  assert.equal(cookie.path, '/')

  await page.locator('#owner').fill('acme-labs')
  await page.getByRole('button', { name: 'Connect Builder' }).click()
  await page.waitForURL(MOCK_BASE + '/onboarding', { timeout: 10000 })
  const afterClientNav = posts(await requests())
  assert.equal(afterClientNav.length, 1)
  assert.match(afterClientNav[0].cookie ?? '', LOGIN_COOKIE)

  // --- The same, starting from /settings and resetting -------------------
  await scenario('default')
  await page.goto(base + '/settings')
  await waitForText(page, 'Settings')
  await page.context().clearCookies()
  await clientNavigate(page, '/reset')
  await waitForText(page, 'Start fresh')
  const reset = await text(page)
  assert.ok(reset.includes('Stop all Forge-managed work'), reset)
  assert.ok(reset.includes('Pause scheduling and terminate every owned worker.'))
  assert.ok(reset.includes('Discard unfinished local work'))
  assert.ok(reset.includes('Remove disposable workspaces and pending factory work. Existing legacy checkouts are preserved.'))
  assert.ok(reset.includes('Clear the current setup'))
  assert.ok(reset.includes('Remove selections, settings and Forge-owned credential copies.'))
  assert.ok(reset.includes('Keep your GitHub work'))
  assert.ok(reset.includes('Issues, commits, branches, PRs and App registrations remain.'))
  assert.ok(reset.includes('Previous lifecycle history is archived. Shared credentials and unrelated host processes remain untouched.'))
  assert.equal(await page.getByRole('link', { name: 'Cancel' }).getAttribute('href'), '/')

  await page.getByRole('button', { name: 'Stop and reset Forge' }).click()
  const requested = await waitForPost(requests, (entry) => entry.body?.action === 'reset', 'reset')
  assert.deepEqual(requested.body, { action: 'reset', generation: 'gen-1' })
  assert.match(requested.cookie ?? '', LOGIN_COOKIE)
}
