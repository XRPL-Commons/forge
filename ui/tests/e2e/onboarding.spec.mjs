import assert from 'node:assert/strict'

const MOCK_BASE = `http://127.0.0.1:${process.env.FORGE_E2E_MOCK_PORT || 8788}`
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
  // Hydration finishes after load; on slow runners the app instance is not attached yet.
  await page.waitForFunction(() => {
    const root = document.querySelector('#__nuxt') ?? document.body.firstElementChild
    return !!(root && root.__vue_app__)
  }, undefined, { timeout: 15000 })
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
  assert.ok(step1.includes('Choose which repositories Forge can access on GitHub.'))
  for (const name of ['Builder', 'Reviewer', 'Auditor', 'Controller']) assert.ok(!step1.includes(name), name)
  assert.ok(step1.includes('Connect on GitHub'))
  assert.ok(step1.includes('Approve access on GitHub to continue.'))
  // Nothing is registered yet, so the rail cannot skip ahead.
  assert.equal(await page.locator('[data-step="1"]').isDisabled(), true)
  assert.equal(await page.locator('[data-step="2"]').isDisabled(), true)

  // Nuxt UI draws its own `i-lucide-*` icons. @nuxt/icon renders them in CSS
  // mode, so the select-menu trigger's chevron is an inline <svg> in a
  // mask-image data URI rather than an <svg> element; an icon that did not
  // resolve leaves no mask at all. It resolving offline is the point: the
  // collection is bundled, and a Forge server has no route to the Iconify
  // API.
  const chevron = page.locator('#account-type [data-slot="trailingIcon"]')
  await chevron.waitFor({ timeout: 10000 })
  const chevronMask = await chevron.evaluate((el) => {
    const style = getComputedStyle(el)
    return style.maskImage || style.webkitMaskImage || ''
  })
  assert.match(chevronMask, /^url\("data:image\/svg\+xml,/, `the Account type chevron did not resolve: ${chevronMask}`)
  assert.ok(chevronMask.includes('%3Csvg'), 'the chevron mask is not an inline SVG')

  // …and the route those icons come from is Nitro's own: proxyPaths()
  // excludes /api/_nuxt_icon, so it is answered from the bundle instead of
  // being forwarded to the Forge status service, which knows nothing of it.
  const iconRoute = await page.evaluate(async (url) => {
    const response = await fetch(url)
    return { status: response.status, body: await response.text() }
  }, base + '/api/_nuxt_icon/lucide.json?icons=chevron-down')
  assert.equal(iconRoute.status, 200, iconRoute.body.slice(0, 200))
  assert.ok(iconRoute.body.includes('"chevron-down"'), iconRoute.body.slice(0, 200))

  await page.locator('#owner').fill('acme-labs')
  await page.locator('#account-type').click()
  await page.getByRole('option', { name: 'Personal account' }).click()
  await page.getByRole('button', { name: 'Connect Forge' }).click()

  // The manifest is handed over by a form POST, and the mock answers 303.
  await page.waitForURL(MOCK_BASE + '/onboarding', { timeout: 10000 })
  const connected = await requests()
  assert.deepEqual(posts(connected)[0].body, {
    action: 'github', role: 'app', owner: 'acme-labs', account_type: 'personal',
  })
  const manifest = posts(connected, '/__manifest')
  assert.equal(manifest.length, 1, 'the manifest form was not submitted to the returned action')
  assert.deepEqual(JSON.parse(manifest[0].body.manifest), { name: 'forge' })

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
  assert.ok(checks.includes('Repository access and existing rules'), checks)
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
  assert.ok(step2.includes('GitHub access ready'))
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
  await page.getByRole('button', { name: 'Connect Forge' }).click()
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
  assert.ok(reset.includes('Remove disposable workspaces and pending factory work.'))
  assert.ok(reset.includes('Clear the current setup'))
  assert.ok(reset.includes('Remove selections, settings and Forge-owned credential copies.'))
  assert.ok(reset.includes('Keep your GitHub work'))
  assert.ok(reset.includes('Issues, commits, branches, PRs and App registrations remain.'))
  assert.ok(reset.includes('Previous lifecycle history is archived. Shared credentials and unrelated host processes remain untouched.'))
  // Cancel returns to the page that led here, not to Activity (#73).
  assert.equal(await page.getByRole('button', { name: 'Cancel' }).count(), 1)

  await page.getByRole('button', { name: 'Stop and reset Forge' }).click()
  const requested = await waitForPost(requests, (entry) => entry.body?.action === 'reset', 'reset')
  assert.deepEqual(requested.body, { action: 'reset', generation: 'gen-1' })
  assert.match(requested.cookie ?? '', LOGIN_COOKIE)
}
