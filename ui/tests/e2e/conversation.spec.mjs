import assert from 'node:assert/strict'
export default async function (page, { base, requests, discussionTime }) {
  await page.addInitScript(() => { Object.defineProperty(crypto, 'randomUUID', { value: undefined, configurable: true }) })
  await page.goto(base + '/issues/1:610')
  // Nothing blocks a running issue: the button asks, it does not call it a blocker.
  assert.equal(await page.getByRole('button', { name: 'Discuss blocker', exact: true }).count(), 0)
  await page.locator('#issue-ask').click()
  const prompt = page.getByRole('textbox', { name: 'Ask about this issue' })
  // Discuss opens the Discussion tab: a full-height chat whose caret
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
  await discussionTime(0)
  await prompt.fill('Look again (hold)')
  await prompt.press('Enter')
  // First Forge looks for someone available, and says why it is still looking.
  const finding = page.locator('[data-presence][data-phase="finding"]')
  await finding.waitFor()
  assert.match(await finding.innerText(), /Looking for someone available/)
  assert.match(await finding.locator('[data-finding-reason]').innerText(), /one runs at a time · waiting \d+ s/)
  await page.screenshot({ path: '/tmp/forge-conversation-finding.png' })
  // Then someone joins, with a face and a name, and one live line says what
  // it is doing now.
  await discussionTime(1300)
  const joined = page.locator('[data-presence][data-phase="joined"]')
  await joined.waitFor()
  assert.match(await joined.locator('[data-presence-title]').innerText(), /^Ember is getting started · \d+ s$/)
  assert.equal(await joined.locator('canvas[data-persona="Ember"]').getAttribute('data-mood'), 'busy')
  // Agents are workers on two feet, not the issues' cube heads.
  const worker = joined.locator('canvas[data-persona="Ember"]')
  assert.equal(await worker.getAttribute('data-shape'), 'worker')
  const workerBox = await worker.boundingBox()
  assert.deepEqual([Math.round(workerBox.width), Math.round(workerBox.height)], [40, 40])
  // The worker is drawn: its feet sit near the bottom of the canvas.
  assert.ok(await worker.evaluate((el) => {
    const data = el.getContext('2d').getImageData(0, Math.floor(el.height * 0.9), el.width, Math.ceil(el.height * 0.05)).data
    return data.some((v, i) => i % 4 === 3 && v > 0)
  }), 'the worker has feet')
  // The line updates in place: only the latest status, never a log.
  await discussionTime(2000)
  await joined.locator('[data-current]').getByText('Ember is reading issue #610').waitFor()
  await discussionTime(2700)
  await joined.locator('[data-current]').getByText('Ember is searching the repository for `producer`').waitFor()
  assert.equal(await joined.locator('[data-presence-title][data-answer-mode="code"]').count(), 1)
  assert.equal(await joined.getByText('Ember is reading issue #610').count(), 0, 'the earlier line is replaced, not kept')
  assert.equal(await joined.locator('[data-current]').count(), 1)
  assert.doesNotMatch(await joined.innerText(), /longer than usual/)
  // The box stays editable so the next question can be drafted meanwhile.
  assert.equal(await prompt.isDisabled(), false)
  assert.equal(await prompt.inputValue(), '')
  await page.screenshot({ path: '/tmp/forge-conversation-replying.png', fullPage: true })
  // Stop is the live control while a reply runs: enabled, not greyed out.
  assert.equal(await page.getByRole('button', { name: 'Stop reply', exact: true }).isEnabled(), true)
  await page.getByRole('button', { name: 'Stop reply', exact: true }).click()
  // Stopping reads as the agent leaving: a system line, not a bubble.
  await page.locator('[data-left][data-reason="cancelled"]').getByText('You stopped Ember').waitFor()
  assert.equal(await page.locator('[data-left] canvas[data-persona="Ember"]').getAttribute('data-mood'), 'left')
  assert.equal(await prompt.isDisabled(), false)
  assert.equal(await page.getByRole('button', { name: 'Cancel reply' }).count(), 0)
  await discussionTime(null)
  await prompt.fill('Why did the last attempt fail?')
  await prompt.press('Enter')
  await page.locator('[data-proposal-action]').waitFor()
  assert.equal((await page.locator('[data-proposal-action]').innerText()).trim(), 'Retry attempt')
  // The answer replaced the live line: Ember's face and name, no join line
  // and no log of what it did.
  assert.equal(await page.locator('[data-joined]').count(), 0)
  assert.equal(await page.locator('[data-presence]').count(), 0)
  const reply = page.locator('[data-role="assistant"]').last()
  assert.equal((await reply.locator('[data-persona-name]').innerText()).trim(), 'Ember')
  assert.match(await reply.innerText(), /Ember via Claude/)
  assert.equal(await reply.locator('canvas[data-persona="Ember"]').getAttribute('data-mood'), 'happy')
  assert.equal(await reply.locator('canvas[data-persona="Ember"]').getAttribute('data-shape'), 'worker')
  // A short reply reads in full: nothing folded.
  assert.equal(await reply.locator('[data-answer]').getAttribute('data-collapsed'), 'false')
  assert.equal(await reply.locator('[data-show-more]').count(), 0)
  // The proposal is one compact card: the button and its short reason, no
  // second "Proposed decision" block repeating the answer.
  const card = reply.locator('[data-proposal]')
  assert.doesNotMatch(await card.innerText(), /Proposed decision/)
  assert.equal(await reply.getByText('Proposed decision').count(), 0)
  assert.equal((await card.locator('[data-proposal-reason]').innerText()).trim(), 'Correct the shared producer type before updating consumers.')
  assert.equal(await card.locator('[data-proposal-action]').count(), 1)
  const cardBox = await card.boundingBox()
  assert.ok(cardBox.height <= 64, `compact proposal card ${cardBox.height}px`)
  assert.equal(await reply.locator('[data-agent-did]').count(), 0)
  assert.doesNotMatch(await reply.innerText(), /What Ember did|Reading issue #610/)
  assert.match(await page.locator('[data-speaker]').innerText(), /Ember answers here, via Claude/)
  // A leave line is a system line, never a user or assistant bubble; the
  // stopped question was never answered, so its line stays.
  assert.equal(await page.locator('[data-role="system"] [data-left]').count(), 1)
  assert.equal(await prompt.inputValue(), '')
  assert.match(await page.locator('body').innerText(), /new conversation using saved issue evidence/i)
  assert.match(await page.locator('body').innerText(), /shared type is incorrect/)
  assert.equal((await requests()).filter(r => r.method === 'POST' && r.path === '/api/issues/action').length, 0)
  // The proposal is the panel's own button: same label, same confirm
  // dialog, with the agent's reason already filled in.
  await page.locator('[data-proposal-action]').click()
  const dialog = page.getByRole('dialog', { name: 'Retry attempt?' })
  await dialog.waitFor()
  const proposedReason = 'Correct the shared producer type before updating consumers.'
  assert.equal(await dialog.getByRole('textbox').inputValue(), proposedReason)
  const messageId = (await reply.locator('[data-answer]').getAttribute('id')).replace(/^answer-/, '')
  const confirmedReason = `${proposedReason} Verify both affected consumers in the same attempt.`
  await dialog.getByRole('textbox').fill(confirmedReason)
  await dialog.getByRole('button', { name: 'Retry attempt', exact: true }).click()
  await dialog.waitFor({ state: 'hidden' })
  const decided = (await discussionPosts()).filter(r => r.body.action === 'apply')
  assert.equal(decided.length, 1)
  assert.deepEqual(decided[0].body, { action: 'apply', key: '1:610', message_id: messageId, reason: confirmedReason })
  assert.equal((await requests()).filter(r => r.method === 'POST' && r.path === '/api/issues/action').length, 0)
  await reply.locator('[data-proposal][data-state="pending"]').waitFor({ timeout: 10000 })
  assert.equal(await card.locator('[data-proposal-action]').count(), 0)
  assert.match(await card.innerText(), /Decision requested/)
  assert.doesNotMatch(await card.innerText(), /Decision applied/)
  const actions = await discussionPosts()
  assert.deepEqual(actions.map(r => r.body.action), ['send', 'cancel', 'send', 'apply'])
  assert.deepEqual(actions.filter(r => r.body.action === 'send').map(r => r.body.message), ['Look again (hold)', 'Why did the last attempt fail?'])
  assert.ok(actions.filter(r => r.body.action === 'send').every(r => typeof r.body.request_id === 'string' && r.body.request_id))
  // The tab is in the URL: a reload lands back in the conversation.
  await page.reload()
  await page.locator(`[id="answer-${messageId}"]`).waitFor()
  await page.locator('[data-proposal][data-state="pending"]').waitFor({ timeout: 10000 })
  assert.equal(await page.locator('[data-proposal-action]').count(), 0, 'reload cannot resubmit a pending proposal')
  assert.equal((await discussionPosts()).filter(r => r.body.action === 'apply').length, 1)
  assert.equal(await page.getByRole('tab', { name: 'Discussion' }).getAttribute('aria-selected'), 'true')
  for (const [width, height] of [[320, 640], [390, 844], [1440, 1000]]) {
    await page.setViewportSize({ width, height })
    await page.waitForTimeout(100)
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `no overflow at ${width}`)
    const box = await prompt.boundingBox()
    assert.ok(box.y + box.height <= height, `prompt on screen at ${width}`)
    await page.screenshot({ path: `/tmp/forge-conversation-${width}.png`, fullPage: true })
  }

  // Over about 90 s, a gentle note on the same line says why it is taking a while.
  await prompt.fill('Check the code properly (slow)')
  await prompt.press('Enter')
  const slow = page.locator('[data-presence][data-phase="joined"] [data-elapsed]')
  await page.waitForFunction(() => /longer than usual/.test(document.querySelector('[data-presence] [data-elapsed]')?.textContent ?? ''))
  assert.equal((await slow.innerText()).trim(), '· 1 min, reading the code, longer than usual')
  await page.screenshot({ path: '/tmp/forge-conversation-slow.png' })
  await page.getByRole('button', { name: 'Stop reply', exact: true }).click()
  await page.locator('[data-left][data-reason="cancelled"]').nth(1).waitFor()

  // A long answer folds behind Show more, and unfolds on request.
  await prompt.fill('Tell me everything (long)')
  await prompt.press('Enter')
  const long = page.locator('[data-role="assistant"]').filter({ hasText: 'Finding 1' })
  await long.waitFor({ timeout: 15000 })
  const body = long.locator('[data-answer]')
  assert.equal(await body.getAttribute('data-collapsed'), 'true')
  const folded = (await body.boundingBox()).height
  assert.ok(folded <= 140, `folded answer ${folded}px`)
  const more = long.getByRole('button', { name: 'Show more' })
  assert.equal(await more.getAttribute('aria-expanded'), 'false')
  await more.click()
  assert.equal(await body.getAttribute('data-collapsed'), 'false')
  assert.ok((await body.boundingBox()).height > folded + 40, 'unfolded answer is taller')
  await long.getByText('The final line of the long answer.').waitFor()
  assert.equal(await long.getByRole('button', { name: 'Show less' }).getAttribute('aria-expanded'), 'true')
  await page.screenshot({ path: '/tmp/forge-conversation-long.png', fullPage: true })
}
