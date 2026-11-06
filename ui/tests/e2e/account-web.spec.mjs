import assert from 'node:assert/strict'

export default async function (page, { base, accounts }) {
  let operation = {}, account = { status: 'connected' }, revision = 0
  const requests = []
  await page.route('**/api/accounts', async route => {
    if (route.request().method() === 'POST') {
      requests.push(route.request().postDataJSON()); revision++
      operation = { status: 'connecting' }
      account = { status: 'account_login_required', code: 'ABCD-12345', url: 'https://auth.openai.com/codex/device', expires_at: Date.now() / 1000 + 900 }
    }
    await route.fulfill({ json: { ok: true, browser_providers: ['codex'], controls_token: 'token', active: true, generation: 'g', revision, setup_revision: 'r', operation, account } })
  })
  await accounts([{ provider: 'codex', name: 'Codex', ready: false, status: 'sign_in_expired', since: Date.now() / 1000,
    until: Date.now() / 1000 + 600, reason_code: 'blocked_auth', waiting_issues: 1 }])
  await page.goto(base + '/settings/harnesses')
  await page.locator('#account-codex').getByRole('button', { name: 'Reconnect Codex' }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Connect Codex' }).click()
  await page.locator('[data-device-code]').waitFor()
  assert.equal(requests[0].action, 'reconnect')
  assert.equal(requests[0].provider, 'codex')
  await page.getByRole('button', { name: 'Close', exact: true }).last().click()
  await page.reload()
  await page.locator('#account-codex').getByRole('button', { name: 'Sign-in progress' }).click()
  await page.locator('[data-device-code]').waitFor()
  assert.equal(requests.length, 1, 'reload resumes the same login')
  for (const width of [320, 390, 1280]) {
    await page.setViewportSize({ width, height: 900 })
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
    await page.getByRole('link', { name: 'Open Codex sign-in' }).waitFor()
    if (width === 320) await page.screenshot({ path: '/private/tmp/forge-account-web-mobile.png' })
  }
  operation = { status: 'failed' }; account = { status: 'account_login_failed' }
  await page.getByRole('dialog').getByRole('button', { name: 'Try again' }).waitFor({ timeout: 10000 }).catch(async error => { console.error(await page.getByRole('dialog').innerText()); throw error })
  await page.getByRole('dialog').getByRole('button', { name: 'Try again' }).click()
  assert.equal(requests.length, 2)
  await page.locator('[data-device-code]').waitFor()
  operation = { status: 'verifying' }; account = { status: 'connected' }
  await page.getByRole('dialog').getByText('Verifying Codex…').waitFor({ timeout: 10000 })
  operation = { status: 'complete' }
  await page.getByRole('dialog').getByText('Codex is connected and verified.').waitFor({ timeout: 10000 })
  assert.equal(await page.getByRole('dialog').getByRole('button', { name: 'Connect Codex' }).count(), 0)
}
