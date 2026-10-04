import assert from 'node:assert/strict'
export default async function (page, { base, requests }) {
  await page.addInitScript(() => { Object.defineProperty(crypto, 'randomUUID', { value: undefined, configurable: true }) })
  await page.goto(base + '/issues/1:610')
  await page.getByRole('button', { name: 'Discuss blocker', exact: true }).click()
  const prompt = page.getByRole('textbox', { name: 'Ask about this issue' })
  // The conversation opens where the operator is: the caret waits in the
  // message box, on screen.
  await page.waitForFunction(() => {
    const box = document.activeElement
    if (!box || box.tagName !== 'TEXTAREA' || !box.closest('#issue-discussion')) return false
    const r = box.getBoundingClientRect()
    return r.top >= 0 && r.bottom <= innerHeight
  }, null, { timeout: 5000 })
  const discussionPosts = async () => (await requests()).filter(r => r.method === 'POST' && r.path === '/api/issues/discussion')
  assert.equal(await prompt.getAttribute('placeholder'), 'Why did the last attempt fail? What would unblock it?')
  // The small submit is disabled until there is something to send.
  assert.equal(await page.getByRole('button', { name: 'Send', exact: true }).isDisabled(), true)
  // Same small size as the issue action row, not an oversized round button.
  const sendBox = await page.getByRole('button', { name: 'Send', exact: true }).boundingBox()
  const discussBox = await page.getByRole('button', { name: 'Discuss blocker', exact: true }).boundingBox()
  assert.ok(sendBox.height <= discussBox.height + 1, `send ${sendBox.height}px vs action row ${discussBox.height}px`)
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
  assert.equal(await prompt.isDisabled(), true)
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
  await page.reload()
  await page.getByRole('button', { name: 'Discuss blocker', exact: true }).click()
  await page.locator('[data-proposal-action]').waitFor()
  for (const width of [320, 390, 1440]) {
    await page.setViewportSize({ width, height: 1000 })
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `no overflow at ${width}`)
    await page.screenshot({ path: `/tmp/forge-conversation-${width}.png`, fullPage: true })
  }
}
