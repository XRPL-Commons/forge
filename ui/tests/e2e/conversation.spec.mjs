import assert from 'node:assert/strict'
export default async function (page, { base, requests }) {
  await page.addInitScript(() => { Object.defineProperty(crypto, 'randomUUID', { value: undefined, configurable: true }) })
  await page.goto(base + '/issues/1:610')
  await page.getByRole('button', { name: 'Discuss blocker', exact: true }).click()
  const prompt = page.getByRole('textbox', { name: 'Ask about this issue' })
  // Discuss blocker opens the Discussion tab: a full-height chat whose caret
  // waits in the message box, on screen, and the URL says so.
  await page.waitForFunction(() => {
    const box = document.activeElement
    if (!box || box.tagName !== 'TEXTAREA' || !box.closest('#issue-discussion')) return false
    const r = box.getBoundingClientRect()
    return r.top >= 0 && r.bottom <= innerHeight
  }, null, { timeout: 5000 })
  assert.equal(await page.getByRole('tab', { name: 'Discussion' }).getAttribute('aria-selected'), 'true')
  await page.waitForURL(/[?&]tab=discussion/)
  assert.equal(await page.locator('#issue-next').isVisible(), false)
  // The issue's header stays above the tabs.
  assert.equal(await page.locator('#issue-title').isVisible(), true)
  assert.equal(await page.locator('#issue-status').isVisible(), true)
  const log = page.getByRole('log', { name: 'Issue conversation' })
  assert.equal(await log.getAttribute('aria-live'), 'polite')
  // Nothing said yet: a few suggested questions.
  assert.equal(await page.locator('[data-suggestions] button').count(), 3)
  const discussionPosts = async () => (await requests()).filter(r => r.method === 'POST' && r.path === '/api/issues/discussion')
  assert.equal(await prompt.getAttribute('placeholder'), 'Why did the last attempt fail? What would unblock it?')
  // The small submit is disabled until there is something to send.
  assert.equal(await page.getByRole('button', { name: 'Send', exact: true }).isDisabled(), true)
  // A small submit, not an oversized round button.
  const sendBox = await page.getByRole('button', { name: 'Send', exact: true }).boundingBox()
  assert.ok(sendBox.height <= 32, `send ${sendBox.height}px`)
  // Full height: the prompt sits at the bottom of the window and the window
  // itself does not scroll.
  const promptBox = await prompt.boundingBox()
  const viewport = page.viewportSize()
  assert.ok(promptBox.y + promptBox.height <= viewport.height && promptBox.y + promptBox.height > viewport.height - 160, `prompt bottom ${promptBox.y + promptBox.height} of ${viewport.height}`)
  assert.ok(await page.evaluate(() => document.documentElement.scrollHeight <= innerHeight + 1), 'the page does not scroll')
  // Shift+Enter adds a line and does not send.
  await prompt.fill('First line')
  await prompt.press('Shift+Enter')
  await prompt.pressSequentially('second line')
  assert.equal(await prompt.inputValue(), 'First line\nsecond line')
  await page.waitForTimeout(300)
  assert.equal((await discussionPosts()).length, 0)
  // Enter sends; while the reply is in progress the submit becomes a stop
  // button that cancels it.
  await prompt.fill('Look again (hold)')
  await prompt.press('Enter')
  await page.getByText('Reading the saved evidence', { exact: true }).waitFor()
  // The streaming indicator says what the worker is doing.
  await page.locator('[data-streaming]').getByText('Agent is reading the saved evidence').waitFor()
  // The box stays editable so the next question can be drafted meanwhile.
  assert.equal(await prompt.isDisabled(), false)
  assert.equal(await prompt.inputValue(), '')
  await page.screenshot({ path: '/tmp/forge-conversation-replying.png', fullPage: true })
  // Stop is the live control while a reply runs: enabled, not greyed out.
  assert.equal(await page.getByRole('button', { name: 'Stop reply', exact: true }).isEnabled(), true)
  await page.getByRole('button', { name: 'Stop reply', exact: true }).click()
  await page.getByText('Reply stopped').waitFor()
  assert.equal(await prompt.isDisabled(), false)
  assert.equal(await page.getByRole('button', { name: 'Cancel reply' }).count(), 0)
  await prompt.fill('Why did the last attempt fail?')
  await prompt.press('Enter')
  await page.locator('[data-proposal-action]').waitFor()
  assert.equal((await page.locator('[data-proposal-action]').innerText()).trim(), 'Retry attempt')
  assert.equal(await prompt.inputValue(), '')
  assert.match(await page.locator('body').innerText(), /new conversation using saved issue evidence/i)
  assert.match(await page.locator('body').innerText(), /shared type is incorrect/)
  assert.equal((await requests()).filter(r => r.method === 'POST' && r.path === '/api/issues/action').length, 0)
  // The proposal is the panel's own button: same label, same confirm
  // dialog, with the agent's reason already filled in.
  await page.locator('[data-proposal-action]').click()
  const dialog = page.getByRole('dialog', { name: 'Retry attempt?' })
  await dialog.waitFor()
  assert.equal(await dialog.getByRole('textbox').inputValue(), 'Correct the shared producer type before updating consumers.')
  await dialog.getByRole('button', { name: 'Retry attempt', exact: true }).click()
  await dialog.waitFor({ state: 'hidden' })
  const decided = (await requests()).filter(r => r.method === 'POST' && r.path === '/api/issues/action')
  assert.equal(decided.length, 1)
  assert.deepEqual([decided[0].body.action, decided[0].body.reason], ['retry', 'Correct the shared producer type before updating consumers.'])
  const actions = await discussionPosts()
  assert.deepEqual(actions.map(r => r.body.action), ['send', 'cancel', 'send'])
  assert.deepEqual(actions.filter(r => r.body.action === 'send').map(r => r.body.message), ['Look again (hold)', 'Why did the last attempt fail?'])
  assert.ok(actions.filter(r => r.body.action === 'send').every(r => typeof r.body.request_id === 'string' && r.body.request_id))
  // The tab is in the URL: a reload lands back in the conversation.
  await page.reload()
  await page.locator('[data-proposal-action]').waitFor()
  assert.equal(await page.getByRole('tab', { name: 'Discussion' }).getAttribute('aria-selected'), 'true')
  for (const [width, height] of [[320, 640], [390, 844], [1440, 1000]]) {
    await page.setViewportSize({ width, height })
    await page.waitForTimeout(100)
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `no overflow at ${width}`)
    const box = await prompt.boundingBox()
    assert.ok(box.y + box.height <= height, `prompt on screen at ${width}`)
    await page.screenshot({ path: `/tmp/forge-conversation-${width}.png`, fullPage: true })
  }
}
