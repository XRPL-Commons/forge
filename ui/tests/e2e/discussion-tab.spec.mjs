import assert from 'node:assert/strict'

// The issue panel's Discussion tab: deep links, tab switching that keeps the
// draft, keyboard tab switching, the tab's dot, the inline failure with Try
// again, and the same chat inside the board's drawer on a phone.
const focusedInChat = () => {
  const box = document.activeElement
  return !!box && box.tagName === 'TEXTAREA' && !!box.closest('#issue-discussion')
}

export default async function (page, { base }) {
  await page.addInitScript(() => { Object.defineProperty(crypto, 'randomUUID', { value: undefined, configurable: true }) })
  // `?tab=discussion` opens straight into the chat, caret in the box.
  await page.goto(base + '/issues/1:610?tab=discussion')
  const discussionTab = page.getByRole('tab', { name: 'Discussion' })
  const detailsTab = page.getByRole('tab', { name: 'Details' })
  assert.equal(await discussionTab.getAttribute('aria-selected'), 'true')
  await page.waitForFunction(focusedInChat, null, { timeout: 5000 })
  const prompt = page.getByRole('textbox', { name: 'Ask about this issue' })

  // A draft survives a trip to Details and back.
  await prompt.fill('A draft I have not sent')
  await detailsTab.click()
  await page.locator('#issue-next').waitFor()
  assert.equal(await prompt.isVisible(), false)
  await page.waitForURL(url => !url.search.includes('tab='))
  await discussionTab.click()
  await page.waitForFunction(focusedInChat, null, { timeout: 5000 })
  assert.equal(await prompt.inputValue(), 'A draft I have not sent')
  await page.waitForURL(/[?&]tab=discussion/)

  // Keyboard: arrows move between the tabs, Enter opens one.
  await detailsTab.click()
  await detailsTab.focus()
  await page.keyboard.press('ArrowRight')
  assert.equal(await page.evaluate(() => document.activeElement?.textContent?.trim().startsWith('Discussion')), true)
  await page.keyboard.press('Enter')
  assert.equal(await discussionTab.getAttribute('aria-selected'), 'true')
  await page.waitForFunction(focusedInChat, null, { timeout: 5000 })

  // A reply that runs out of time: the agent leaves, says why, and offers Try again.
  await prompt.fill('What changed? (fail)')
  await prompt.press('Enter')
  const left = page.locator('[data-left][data-reason="timeout"]')
  await left.waitFor()
  assert.match(await left.innerText(), /Ember ran out of time after 10 min/)
  await left.getByRole('button', { name: 'Try again' }).click()
  await page.locator('[data-presence]').waitFor()
  await page.locator('[data-proposal-action]').waitFor()
  assert.equal(await left.getByRole('button', { name: 'Try again' }).count(), 0)
  assert.equal(await page.locator('[data-role="user"]').filter({ hasText: 'What changed? (fail)' }).count(), 1)

  // The same words again after a failure are Try again, never a second copy.
  await prompt.fill('And now? (fail)')
  await prompt.press('Enter')
  await page.locator('[data-left][data-reason="timeout"]').nth(1).waitFor()
  await prompt.fill('And now? (fail)')
  await prompt.press('Enter')
  await page.locator('[data-presence]').waitFor()
  assert.equal(await page.locator('[data-role="user"]').filter({ hasText: 'And now? (fail)' }).count(), 1)
  await page.locator('[data-proposal-action]').nth(1).waitFor()
  assert.equal(await prompt.inputValue(), '')

  // A provider that cannot sign in says so truthfully, and someone else joins.
  await prompt.fill('Who is answering? (auth)')
  await prompt.press('Enter')
  const auth = page.locator('[data-left][data-reason="auth"]')
  await auth.waitFor({ timeout: 15000 })
  assert.match(await auth.innerText(), /Bolt can't sign in: the Codex login expired\. Reconnect it in Settings → Coding accounts\. · finding someone else/)
  assert.doesNotMatch(await auth.innerText(), /quota|usage limit/i)
  assert.equal(await auth.getByRole('link', { name: 'Settings → Coding accounts' }).getAttribute('href'), '/settings/harnesses')
  assert.equal(await auth.getByRole('button', { name: 'Try again' }).count(), 0)
  assert.equal(await auth.locator('canvas[data-persona="Bolt"]').count(), 1)
  await page.locator('[data-proposal-action]').nth(2).waitFor({ timeout: 20000 })
  assert.equal((await page.locator('[data-joined]').last().innerText()).trim(), 'Ember joined instead · Claude Sonnet 4.6')
  assert.equal((await page.locator('[data-role="assistant"]').last().locator('[data-persona-name]').innerText()).trim(), 'Ember')

  // A reply in progress shows on the tab while the operator is on Details.
  // With reduced motion the avatars hold still and the chat still works.
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await prompt.fill('Keep looking (hold)')
  await prompt.press('Enter')
  await page.locator('[data-presence][data-phase="joined"]').waitFor()
  await detailsTab.click()
  await page.locator('#discussion-tab-dot').getByText('(reply in progress)').waitFor()
  await discussionTab.click()
  assert.equal(await page.locator('#discussion-tab-dot').count(), 0)
  await page.getByRole('button', { name: 'Stop reply', exact: true }).click()
  await page.getByText('You stopped Ember').waitFor()

  // The board's drawer on a phone: the same tabs, a chat that fills the
  // drawer, the prompt on screen and nothing scrolling sideways.
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(base + '/?issue=1:610')
  const drawer = page.getByRole('dialog')
  await drawer.locator('#issue-ask').click()
  await page.waitForFunction(focusedInChat, null, { timeout: 5000 })
  await page.waitForURL(/[?&]tab=discussion/)
  assert.equal(await drawer.getByRole('tab', { name: 'Discussion' }).getAttribute('aria-selected'), 'true')
  assert.equal(await page.getByRole('link', { name: /Open full page/ }).getAttribute('href'), '/issues/1%3A610?tab=discussion')
  const box = await drawer.getByRole('textbox', { name: 'Ask about this issue' }).boundingBox()
  assert.ok(box.y + box.height <= 844 && box.y + box.height > 844 - 160, `drawer prompt bottom ${box.y + box.height}`)
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'no overflow in the drawer')
  await page.screenshot({ path: '/tmp/forge-discussion-drawer-390.png' })
}
