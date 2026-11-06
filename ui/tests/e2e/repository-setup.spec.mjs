import assert from 'node:assert/strict'

const posted = path => entry => entry.method === 'POST' && entry.path === path
async function waitText(page, text) {
  await page.waitForFunction(text => document.body.innerText.includes(text), text)
}
async function waitStep(page, name) {
  const dialog = page.getByRole('dialog')
  if (name === 'Repository') await dialog.getByRole('searchbox', { name: 'Find by name' }).waitFor()
  else if (name === 'Merge policy') await dialog.getByRole('button', { name: 'Check again', exact: true }).waitFor()
  else if (name === 'App access') await dialog.getByRole('heading', { name: 'GitHub access', exact: true }).waitFor()
  else await dialog.getByRole('button', { name: 'Enable automation', exact: true }).waitFor()
}
// A click returns before the request it sends is recorded, so anything read
// after one is polled until it settles (10 s), never read once.
async function until(read, done) {
  for (const deadline = Date.now() + 10000; ;) {
    const value = await read()
    if (done(value) || Date.now() > deadline) return value
    await new Promise((resolve) => setTimeout(resolve, 50))
  }
}
function posts(requests, path, count = 1) {
  return until(async () => (await requests()).filter(posted(path)), list => list.length >= count)
}
async function hydrated(page) {
  await page.waitForFunction(() => document.querySelector('#__nuxt')?.__vue_app__?.config.globalProperties.$nuxt?.isHydrating === false)
}

export default async function (page, { base, scenario, requests }) {
  await page.goto(base + '/settings/repositories')
  await hydrated(page)
  await waitText(page, '1 enabled · 2 discovered or configured')
  assert.equal(await page.title(), 'Repositories · Forge')
  assert.equal(await page.getByRole('link', { name: 'Repositories', exact: true }).getAttribute('aria-current'), 'page')
  assert.equal(await page.getByRole('link', { name: 'General', exact: true }).getAttribute('aria-current'), null)
  assert.equal(await page.locator('#connections').count(), 0)
  assert.ok((await page.locator('[data-key="1"]').innerText()).includes('Enabled'))
  assert.ok((await page.locator('[data-key="2"]').innerText()).includes('Not enabled'))
  await page.getByRole('searchbox', { name: 'Find a repository' }).fill('hack')
  assert.equal(await until(() => page.locator('tbody tr').count(), count => count === 1), 1)
  await page.getByRole('searchbox', { name: 'Find a repository' }).fill('')

  // Keyboard-only entry, selection, Back and Cancel. Already enabled repos stay visible.
  const add = page.getByRole('button', { name: 'Add repository', exact: true })
  await add.focus(); await page.keyboard.press('Enter')
  let dialog = page.getByRole('dialog')
  await waitStep(page, 'Repository')
  await dialog.getByRole('radio', { name: /hackathon/ }).waitFor()
  assert.equal(await dialog.getByRole('radio').count(), 2)
  assert.equal(await dialog.getByRole('radio', { name: /academy/ }).isDisabled(), true)
  assert.equal(await dialog.getByRole('button', { name: 'Continue', exact: true }).isDisabled(), true)
  await dialog.getByRole('radio', { name: /hackathon/ }).focus(); await page.keyboard.press('Space')
  await dialog.getByRole('button', { name: 'Continue', exact: true }).focus(); await page.keyboard.press('Enter')
  await waitStep(page, 'Enable')
  await dialog.getByRole('button', { name: 'Back', exact: true }).focus(); await page.keyboard.press('Enter')
  await waitStep(page, 'Repository')
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).focus(); await page.keyboard.press('Enter')
  await dialog.waitFor({ state: 'detached' })
  assert.equal(await add.evaluate(el => el === document.activeElement), true)
  assert.equal((await requests()).filter(posted('/api/repositories/enable')).length, 0)

  await add.click()
  await dialog.getByRole('searchbox', { name: 'Find by name' }).fill('missing')
  await waitText(page, 'No matching repositories yet.')
  await dialog.getByRole('searchbox', { name: 'Find by name' }).fill('hack')
  await dialog.getByRole('radio', { name: /hackathon/ }).check()
  await dialog.getByRole('button', { name: 'Continue', exact: true }).click()
  await waitStep(page, 'Enable')

  assert.equal(await dialog.locator('#deployment-title, #test-databases').count(), 0)
  assert.equal((await requests()).filter(entry => entry.path.startsWith('/api/deployment')).length, 0)
  await dialog.getByRole('button', { name: 'Enable automation', exact: true }).click()
  await page.waitForURL(base + '/settings/repositories/2')
  await dialog.waitFor({ state: 'detached' })
  await page.getByRole('switch', { name: 'Enable automation', exact: true }).waitFor()
  assert.equal(await page.getByRole('switch', { name: 'Enable automation', exact: true }).getAttribute('aria-checked'), 'true')
  const enables = await posts(requests, '/api/repositories/enable')
  assert.equal(enables.length, 1)
  assert.deepEqual(enables[0].body, { repo_id: 2, revision: 'settings-rev-1' })

  // Repository verification observes owner rules without offering imports or bypass repairs.
  await scenario('needs-setup')
  await page.goto(base + '/settings/repositories/2?setup=1')
  await waitStep(page, 'Merge policy')
  dialog = page.getByRole('dialog')
  assert.equal(await dialog.locator('[data-requirement-fix], [data-rule-fix], #download, #github-protection').count(), 0)
  assert.match(await dialog.innerText(), /The supervisor merges Forge work after CI, independent Review and Audit pass/)
  assert.equal(await dialog.getByRole('link', { name: 'View GitHub rules ↗' }).getAttribute('href'), 'https://github.com/XRPL-Commons/hackathon/settings/rules')
  await dialog.getByRole('button', { name: 'Check again', exact: true }).click()
  const checks = await posts(requests, '/api/repositories/check')
  assert.equal(checks.length, 1)
  assert.deepEqual(checks[0].body, { repo_id: 2, revision: 'settings-rev-1' })
  for (const width of [320, 390]) {
    await page.setViewportSize({ width, height: 844 })
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    const button = await dialog.getByRole('button', { name: 'Check again', exact: true }).boundingBox()
    assert.ok(button.y >= 0 && button.y + button.height <= 844, 'primary action stays visible on mobile')
  }
  await page.screenshot({ path: '/tmp/forge-repository-wizard-mobile.png', fullPage: true })
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click()
  await dialog.waitFor({ state: 'detached' })

  // Stale evidence triggers the existing bounded automatic refresh.
  await page.goto('about:blank'); await scenario('stale')
  await page.goto(base + '/settings/repositories/2')
  await waitText(page, 'Check again to confirm the current GitHub settings')
  await posts(requests, '/api/repositories/check')
  await page.waitForTimeout(700)
  assert.equal((await requests()).filter(posted('/api/repositories/check')).length, 1)

  // Existing classic and ruleset observations are read-only even when old cached
  // observations include a missing bypass. No generated mutation command is shown.
  for (const name of ['bypass-missing', 'bypass-classic']) {
    await page.goto('about:blank'); await scenario(name)
    await page.goto(base + '/settings/repositories/2?setup=1')
    await waitStep(page, 'Merge policy')
    dialog = page.getByRole('dialog')
    assert.equal(await dialog.locator('[data-rule-fix], #bypass-command, #download').count(), 0)
    assert.match(await dialog.locator('#github-rules').innerText(), /Repository owners manage these rules/)
    await dialog.getByRole('button', { name: 'Cancel', exact: true }).click()
    await dialog.waitFor({ state: 'detached' })
  }

  // A refused check is retried: the page shows the reason, and once the server
  // accepts checks again (same payload, no reload) the next tick asks again.
  await page.goto('about:blank')
  await scenario('check-rejected')
  const retries = []
  const onRetry = (request) => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/repositories/check') retries.push(request.postDataJSON())
  }
  page.on('request', onRetry)
  await page.goto(base + '/settings/repositories/2')
  await waitText(page, 'Could not start the check.')
  assert.equal(retries.length, 1, 'the refused check was attempted once')
  await scenario('stale')
  for (const deadline = Date.now() + 10000; retries.length < 2 && Date.now() < deadline;) await new Promise((resolve) => setTimeout(resolve, 50))
  assert.equal(retries.length, 2, 'the check is retried after a refusal')
  page.off('request', onRetry)

  // Access blockers keep the operator on that step and expose the GitHub fix.
  await page.goto('about:blank'); await scenario('access-needed')
  await page.goto(base + '/settings/repositories/2?setup=1')
  await waitStep(page, 'App access')
  assert.equal(await dialog.getByRole('button', { name: 'Enable automation', exact: true }).count(), 0)
  assert.equal(await dialog.getByRole('link', { name: 'Manage access ↗' }).count(), 1)
  await dialog.getByRole('button', { name: 'Check again', exact: true }).click()
  assert.equal((await posts(requests, '/api/settings/sync')).length, 1)
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click()
  await dialog.waitFor({ state: 'detached' })
  assert.equal((await requests()).filter(posted('/api/repositories/enable')).length, 0)

  // A refused enable keeps the wizard open, with the server's reason and an exit.
  await page.goto('about:blank'); await scenario('enable-rejected')
  await page.goto(base + '/settings/repositories/2?setup=1')
  await waitStep(page, 'Enable')
  await dialog.getByRole('button', { name: 'Enable automation', exact: true }).click()
  await waitText(page, 'Repository settings changed. Check again.')
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click()
  await dialog.waitFor({ state: 'detached' })
  assert.equal((await posts(requests, '/api/repositories/enable')).length, 1)

  // A list row opens setup without enabling; disabling retains its existing confirmation.
  await page.goto('about:blank'); await scenario('default')
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.goto(base + '/settings/repositories')
  await hydrated(page); await waitText(page, '1 enabled · 2 discovered or configured')
  await page.locator('[data-key="2"]').getByRole('switch').click()
  await waitStep(page, 'Enable')
  await page.keyboard.press('Escape'); await dialog.waitFor({ state: 'detached' })
  await page.getByRole('button', { name: 'Refresh repositories', exact: true }).click()
  const allChecks = await posts(requests, '/api/repositories/check')
  assert.deepEqual(allChecks[0].body, { repo_id: null, revision: 'settings-rev-1' })
  await page.locator('[data-key="1"]').getByRole('switch').click()
  await page.getByRole('button', { name: 'Close', exact: true }).click()
  await page.getByRole('dialog').waitFor({ state: 'detached' })
  assert.equal((await requests()).filter(posted('/api/repositories/disable')).length, 0)
  await page.locator('[data-key="1"]').getByRole('switch').click()
  await page.getByRole('button', { name: 'Disable', exact: true }).click()
  await waitText(page, 'Settings saved.')
  const saves = await posts(requests, '/api/repositories/disable')
  assert.equal(saves.length, 1)
  assert.deepEqual(saves[0].body, { repo_id: 1, revision: 'settings-rev-1' })
  assert.equal((await requests()).filter(posted('/api/settings')).length, 0)
  await page.getByRole('dialog').waitFor({ state: 'detached' })
  await waitText(page, '0 enabled · 2 discovered or configured')
  const switchState = () => page.locator('[data-key="1"]').getByRole('switch').getAttribute('aria-checked')
  assert.equal(await until(switchState, state => state === 'false'), 'false')
  await page.screenshot({ path: '/tmp/forge-repositories-desktop.png', fullPage: true })

  await page.goto('about:blank'); await scenario('disable-rejected')
  await page.goto(base + '/settings/repositories')
  await hydrated(page); await waitText(page, '1 enabled · 2 discovered or configured')
  await page.locator('[data-key="1"]').getByRole('switch').click()
  await page.getByRole('button', { name: 'Disable', exact: true }).click()
  await waitText(page, 'Repository settings changed. Reload before disabling.')
  assert.equal(await until(switchState, state => state === 'true'), 'true')
  assert.equal((await requests()).filter(posted('/api/settings')).length, 0)

}
