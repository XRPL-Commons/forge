import assert from 'node:assert/strict'
import { assertPrimaryFarRight } from './dialog-footer.mjs'

// Closing from a discussion: the agent may propose Close issue, and the
// operator applies it with the very same confirm dialog; when the operator
// asks in words, the agent closes it for them and the reply offers Undo. A
// decision Forge does not offer never ends the conversation.
export default async function (page, { base, scenario, requests }) {
  await page.addInitScript(() => { Object.defineProperty(crypto, 'randomUUID', { value: undefined, configurable: true }) })
  await scenario('close-issue')
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.goto(base + '/issues/1:620')
  await page.locator('#issue-workbench').waitFor()
  // The speech bubble and its tail are a solid surface, border included.
  const paint = await page.locator('#issue-bubble').evaluate((el) => {
    const alpha = (color) => { const m = color.match(/\/\s*([\d.]+)\)|rgba\([^)]*,\s*([\d.]+)\)/); return m ? Number(m[1] ?? m[2]) : 1 }
    const own = getComputedStyle(el); const tail = getComputedStyle(el, '::before')
    return [own.backgroundColor, own.borderTopColor, tail.backgroundColor, tail.borderLeftColor].map(alpha)
  })
  assert.deepEqual(paint, [1, 1, 1, 1], 'opaque bubble, tail and state border')

  await page.goto(base + '/issues/1:620?tab=discussion')
  const prompt = page.getByRole('textbox', { name: 'Ask about this issue' })
  await prompt.waitFor()
  const ask = async (text) => { await prompt.fill(text); await prompt.press('Enter') }

  // The agent's own suggestion is a card to click, never a close.
  await ask('Should we keep going? (suggest close)')
  const suggested = page.locator('[data-role="assistant"]').filter({ hasText: 'I would close it.' })
  await suggested.waitFor({ timeout: 15000 })
  const card = suggested.locator('[data-proposal]')
  assert.equal(await card.getAttribute('data-state'), 'offer')
  assert.equal((await card.locator('[data-proposal-action]').innerText()).trim(), 'Close issue…')
  await card.locator('[data-proposal-action]').click()
  const dialog = page.getByRole('dialog', { name: 'Close issue #620?' })
  await dialog.waitFor()
  assert.equal(await dialog.getByRole('textbox', { name: 'Why are you closing it?' }).inputValue(),
    'The remaining work needs GitHub App workflow permissions, which the operator does not want to grant.')
  await assertPrimaryFarRight(dialog, { primary: 'Close issue', secondary: 'Cancel' })
  await dialog.getByRole('button', { name: 'Close issue', exact: true }).click()
  await dialog.waitFor({ state: 'detached' })
  const closes = (await requests()).filter(r => r.method === 'POST' && r.path === '/api/issues/action')
  assert.deepEqual(closes.map(r => [r.body.action, r.body.reason]),
    [['close', 'The remaining work needs GitHub App workflow permissions, which the operator does not want to grant.']])

  // Asked in words: closed for the operator, with Undo.
  await ask('I dont want to edit workflow, close this issue (close)')
  const closed = page.locator('[data-role="assistant"]').filter({ hasText: 'You can undo this.' })
  await closed.waitFor({ timeout: 15000 })
  const done = closed.locator('[data-proposal]')
  assert.equal(await done.getAttribute('data-state'), 'closed')
  assert.match(await done.innerText(), /Closed as no longer needed, as you asked\./)
  assert.equal(await done.locator('[data-proposal-action]').count(), 0)
  await done.locator('[data-undo]').click()
  await page.waitForFunction(() => document.querySelector('[data-proposal][data-state="reopened"]'))
  const undo = (await requests()).filter(r => r.method === 'POST' && r.path === '/api/issues/discussion' && r.body.action === 'undo')
  assert.equal(undo.length, 1)
  assert.equal(undo[0].body.key, '1:620')
  assert.match(await closed.locator('[data-proposal]').innerText(), /Reopened\./)

  // A decision Forge does not offer: the answer still arrives, without a button.
  await ask('Can you merge it? (unavailable)')
  const plain = page.locator('[data-role="assistant"]').filter({ hasText: 'I would merge it as it is.' })
  await plain.waitFor({ timeout: 15000 })
  assert.equal(await plain.locator('[data-proposal]').count(), 0)
  assert.equal((await plain.locator('[data-unavailable]').innerText()).trim(), "That action isn't available here.")
  assert.equal(await page.locator('[data-left]').count(), 0, 'no dead end')
  assert.equal(await page.getByRole('button', { name: 'Try again' }).count(), 0)
}
