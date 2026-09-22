import assert from 'node:assert/strict'

const GROK_LOGIN = '/opt/forge/bin/forge setup login grok --root /srv/forge'
const CHECK_COMMAND = '/opt/forge/bin/forge setup check --root /srv/forge'
const SANDBOX_ERROR = 'The claude native tool sandbox failed verification inside the runner. Check the runner image and the host isolation policies'

// textContent, not innerText: the eyebrows are styled uppercase, and these
// assertions are about the copy, not about the CSS.
function waitForText(page, text) {
  return page.waitForFunction((needle) => document.body.textContent.includes(needle), text, { timeout: 10000 })
}

function text(page) {
  return page.evaluate(() => document.body.textContent)
}

// End-to-end behaviour of Settings · Coding accounts: one row per harness,
// the two terminal commands behind one dialog, the copy fallback that is the
// normal case over plain HTTP, and the page-level error notice.
export default async function (page, { base, scenario }) {
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write'], { origin: base })
  await page.goto(base + '/settings/harnesses')
  assert.equal(await page.title(), 'Coding accounts · Forge')
  await waitForText(page, 'Manage sign-in from the Forge VM terminal.')

  // Three harnesses, each reading its own state (contracts §3.7).
  assert.equal(await page.locator('[data-harness]').count(), 3)
  const listed = await text(page)
  assert.ok(listed.includes('Codex'), listed)
  assert.ok(listed.includes('Claude Code'))
  assert.ok(listed.includes('Grok'))
  assert.ok(listed.includes('gpt-5-codex'), 'a fully verified harness names its model without a suffix')
  assert.ok(!listed.includes('gpt-5-codex · '), listed)
  assert.ok(listed.includes('Not connected'))
  assert.ok(listed.includes('Connected harnesses and models take turns across all roles. If one is unavailable, Forge uses another.'))
  assert.equal(await page.locator('[data-harness="codex"]').getByText('Verified').count(), 1)
  assert.equal(await page.locator('[data-harness="claude"]').getByText('Verified').count(), 1)
  // Nothing failed in this scenario, so there is no error notice.
  assert.ok(!listed.includes(SANDBOX_ERROR))

  // Connect opens the dialog with both commands.
  await page.locator('[data-harness="grok"]').getByRole('button', { name: 'Connect' }).click()
  await waitForText(page, 'Connect Grok')
  const dialog = await text(page)
  assert.ok(dialog.includes('Run this command in a terminal on the Forge VM, as the Forge service account. Follow the harness’s sign-in instructions.'), dialog)
  assert.ok(dialog.includes(GROK_LOGIN))
  assert.ok(dialog.includes('After signing in, verify the connection:'))
  assert.ok(dialog.includes(CHECK_COMMAND))
  assert.ok(dialog.includes('Account status updates automatically after verification.'))

  // Copying works where the Clipboard API is available.
  await page.getByRole('button', { name: 'Copy sign-in command' }).click()
  await waitForText(page, 'Copied')
  assert.equal(await page.evaluate(() => navigator.clipboard.readText()), GROK_LOGIN)
  await page.getByRole('button', { name: 'Close' }).click()

  // Over plain HTTP navigator.clipboard is undefined: the operator is asked
  // to copy the selected command instead.
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, get: () => undefined })
  })
  await page.reload()
  await waitForText(page, 'Manage sign-in from the Forge VM terminal.')
  await page.locator('[data-harness="grok"]').getByRole('button', { name: 'Connect' }).click()
  await waitForText(page, 'Connect Grok')
  await page.getByRole('button', { name: 'Copy sign-in command' }).click()
  await waitForText(page, 'Select and copy the command above')
  assert.equal(await page.evaluate(() => window.getSelection().toString()), GROK_LOGIN, 'the command is left selected')
  await page.getByRole('button', { name: 'Close' }).click()

  // A failed provider check surfaces the error on the page — never inside a
  // harness row — and the next poll brings it in without a reload.
  await scenario('provider-failed')
  await waitForText(page, SANDBOX_ERROR)
  const failed = await text(page)
  assert.ok(failed.includes('claude-sonnet-4.5 · Verification failed'), failed)
  assert.ok(failed.includes('gpt-5-codex'), 'the passing harness keeps its plain model line')
  const notice = page.getByText(SANDBOX_ERROR)
  assert.equal(await notice.count(), 1)
  assert.equal(await page.locator('[data-harness]').getByText(SANDBOX_ERROR).count(), 0)

  // A connected harness whose model did not pass asks to verify, and its
  // dialog leaves out the sign-in block.
  await page.locator('[data-harness="claude"]').getByRole('button', { name: 'Verify connection' }).click()
  await waitForText(page, 'Verify Claude Code')
  const verify = await text(page)
  assert.ok(verify.includes('Run this command in a terminal on the Forge VM, as the Forge service account, to verify the connection:'), verify)
  assert.ok(!verify.includes('Copy sign-in command'), 'a connected harness is not asked to sign in again')
  assert.ok(!verify.includes('Follow the harness’s sign-in instructions.'))
  assert.ok(verify.includes(CHECK_COMMAND))
  await page.getByRole('button', { name: 'Close' }).click()
}
