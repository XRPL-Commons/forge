import assert from 'node:assert/strict'

const BAR = '[data-account-alerts]'
const STRIP = '.factory-line canvas[role="img"]'
const PILL = '[data-accounts-pill]'
const PANEL = '[role="dialog"][data-side="right"]'
// 22:25 local time today, so the copy reads "at 22:25" in any timezone.
const SINCE = (() => { const d = new Date(); d.setHours(22, 25, 0, 0); return Math.floor(d.getTime() / 1000) })()

const DOWN = [
  { provider: 'claude', name: 'Claude', ready: true, status: 'ok', since: null, until: null, reason_code: null, detail: null, waiting_issues: 0 },
  { provider: 'codex', name: 'Codex', ready: false, status: 'sign_in_expired', since: SINCE, until: SINCE + 600, reason_code: 'blocked_auth', detail: null, waiting_issues: 5 },
]

async function headline(page) {
  await page.locator(STRIP).waitFor({ timeout: 10000 })
  return page.locator(STRIP).getAttribute('aria-label')
}

// A signed-out coding account is front and centre on every page: the alert
// bar under the navigation (error colour, exact copy, a deep link to where
// it is reconnected), the header chip and the factory strip's headline. A
// healthy pool shows none of it.
export default async function (page, { base, accounts }) {
  await page.setViewportSize({ width: 1280, height: 900 })

  // Healthy: nothing.
  await page.goto(base + '/')
  assert.equal(await headline(page), '2 need you · 1 of 4 slots in use')
  assert.equal(await page.locator(BAR).count(), 0)
  assert.equal(await page.locator(PILL).count(), 0)

  await accounts(DOWN)
  await page.goto(base + '/')
  const alert = page.locator(`${BAR} [data-account-alert="codex"]`)
  await alert.waitFor({ timeout: 10000 })
  assert.equal(await alert.getAttribute('data-status'), 'sign_in_expired')
  assert.equal(await alert.getAttribute('role'), 'alert')
  const text = (await alert.innerText()).replace(/\s+/g, ' ')
  assert.match(text, /Codex can't sign in: its login expired at (22:25|10:25\sPM)\./)
  assert.match(text, /5 issues are waiting on it\. Claude is covering what it can\./)
  assert.doesNotMatch(text, /usage limit/i)
  const reconnect = alert.getByRole('link', { name: 'Reconnect Codex' })
  assert.equal(await reconnect.getAttribute('href'), '/settings/harnesses#account-codex')
  // UAlert's error colour.
  assert.match(await alert.getAttribute('class'), /error/)

  // Header chip and strip headline.
  assert.equal((await page.locator(PILL).innerText()).trim(), '1 account down')
  await page.waitForFunction((s) => document.querySelector(s)?.getAttribute('aria-label')?.startsWith('Codex signed out'), STRIP, { timeout: 10000 })
  assert.equal(await headline(page), 'Codex signed out · 2 need you · 1 of 4 slots in use')

  // Issues page and the issue drawer carry it too.
  await page.goto(base + '/issues')
  await page.locator(`${BAR} [data-account-alert="codex"]`).waitFor({ timeout: 10000 })
  await page.goto(base + '/?issue=1:601')
  await page.locator(`${PANEL} [data-account-alert="codex"]`).waitFor({ timeout: 10000 })
  await page.keyboard.press('Escape')

  // The deep link lands on the account row on Settings · Coding accounts,
  // which leads with the same status and its reconnect action.
  await page.goto(base + '/')
  await page.locator(`${BAR} [data-account-alert="codex"]`).getByRole('link', { name: 'Reconnect Codex' }).click()
  await page.waitForURL(base + '/settings/harnesses#account-codex')
  const top = page.locator('[data-settings-account-alert="codex"]')
  await top.waitFor({ timeout: 10000 })
  assert.match((await top.innerText()).replace(/\s+/g, ' '), /Codex can't sign in/)
  await page.locator('#account-codex [data-account-status]').waitFor({ timeout: 10000 })
  assert.equal((await page.locator('#account-codex [data-account-status]').innerText()).trim(), 'Signed out')
  await top.getByRole('button', { name: 'Reconnect Codex' }).click()
  await page.getByRole('dialog').waitFor({ timeout: 10000 })
  await page.keyboard.press('Escape')

  // Dismissed for the session; it returns when another account fails.
  await page.goto(base + '/')
  await page.locator(`${BAR} [data-account-alert="codex"]`).waitFor({ timeout: 10000 })
  await page.locator(`${BAR} [data-account-alert="codex"]`).getByRole('button', { name: 'Close' }).click()
  await page.locator(BAR).waitFor({ state: 'detached', timeout: 10000 })
  await page.reload()
  await page.locator(PILL).waitFor({ timeout: 10000 })
  assert.equal(await page.locator(BAR).count(), 0)
  await accounts([{ ...DOWN[0], ready: false, status: 'quota', reason_code: 'blocked_quota', since: SINCE, until: SINCE + 600 }, DOWN[1]])
  await page.reload()
  const quota = page.locator(`${BAR} [data-account-alert="claude"]`)
  await quota.waitFor({ timeout: 10000 })
  assert.match(await quota.getAttribute('class'), /warning/)
  assert.equal((await page.locator(PILL).innerText()).trim(), '2 accounts down')

  // Healthy again: nothing.
  await accounts(DOWN.map(row => ({ ...row, ready: true, status: 'ok', since: null, until: null, reason_code: null, waiting_issues: 0 })))
  await page.reload()
  await headline(page)
  await page.waitForTimeout(300)
  assert.equal(await page.locator(BAR).count(), 0)
  assert.equal(await page.locator(PILL).count(), 0)
}
