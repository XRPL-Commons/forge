import assert from 'node:assert/strict'
import { assertPrimaryFarRight } from './dialog-footer.mjs'

// Confirmation records the discussion's stable request. Only canonical
// GitHub outcomes can show Closed or Reopened and make Undo available.
export default async function (page, { base, scenario, requests, observeDiscussion }) {
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
  const reason = 'The operator confirmed that this work is no longer needed.'
  await dialog.getByRole('textbox', { name: 'Why are you closing it?' }).fill(reason)
  await dialog.getByRole('button', { name: 'Close issue', exact: true }).click()
  await dialog.waitFor({ state: 'detached' })
  const applies = (await requests()).filter(r => r.method === 'POST' && r.path === '/api/issues/discussion' && r.body.action === 'apply')
  assert.equal(applies.length, 1)
  const suggestedId = (await suggested.locator('[data-answer]').getAttribute('id')).replace(/^answer-/, '')
  assert.deepEqual(applies[0].body, { action: 'apply', key: '1:620', message_id: suggestedId, reason })
  assert.equal((await requests()).filter(r => r.method === 'POST' && r.path === '/api/issues/action').length, 0)
  await suggested.locator('[data-state="pending"]').waitFor({ timeout: 10000 })
  assert.equal(await card.locator('[data-undo]').count(), 0)
  await observeDiscussion(suggestedId, { request_id: suggestedId, status: 'rejected', message: 'The issue changed before this request could apply.', reversible: false, undo: null })
  await suggested.locator('[data-state="failed"]').waitFor({ timeout: 10000 })
  assert.match(await card.innerText(), /issue changed/)
  assert.equal(await card.locator('[data-undo]').count(), 0)

  // Explicit wording requests closure; a successful POST is not closure.
  await ask('Close this issue.')
  const closed = page.locator('[data-role="assistant"]').filter({ hasText: 'Requested closure as no longer needed.' })
  await closed.waitFor({ timeout: 15000 })
  const done = closed.locator('[data-proposal]')
  const closedId = (await closed.locator('[data-answer]').getAttribute('id')).replace(/^answer-/, '')
  assert.equal(await done.getAttribute('data-state'), 'pending')
  assert.equal(await done.locator('[data-undo]').count(), 0)
  const applied = { request_id: 'close-receipt', status: 'applied', message: null, reversible: false, undo: null }
  await observeDiscussion(closedId, applied)
  await closed.locator('[data-state="closed"]').waitFor({ timeout: 10000 })
  assert.match(await done.innerText(), /Closed as no longer needed\./)
  assert.equal(await done.locator('[data-undo]').count(), 0, 'a superseded closure is not reversible')
  await observeDiscussion(closedId, { ...applied, reversible: true })
  await done.locator('[data-undo]').waitFor({ timeout: 10000 })
  await done.locator('[data-undo]').click()
  await closed.locator('[data-state="undo_pending"]').waitFor({ timeout: 10000 })
  assert.equal(await done.locator('[data-undo]').count(), 0)
  assert.doesNotMatch(await done.innerText(), /Reopened\./)
  const undo = (await requests()).filter(r => r.method === 'POST' && r.path === '/api/issues/discussion' && r.body.action === 'undo')
  assert.equal(undo.length, 1)
  assert.equal(undo[0].body.key, '1:620')
  await observeDiscussion(closedId, { ...applied, undo: { request_id: 'undo-request', status: 'applied', message: null } })
  await closed.locator('[data-state="reopened"]').waitFor({ timeout: 10000 })
  assert.match(await done.innerText(), /Reopened\./)

  // A decision Forge does not offer: the answer still arrives, without a button.
  await ask('Can you merge it? (unavailable)')
  const plain = page.locator('[data-role="assistant"]').filter({ hasText: 'I would merge it as it is.' })
  await plain.waitFor({ timeout: 15000 })
  assert.equal(await plain.locator('[data-proposal]').count(), 0)
  assert.equal((await plain.locator('[data-unavailable]').innerText()).trim(), "That action isn't available here.")
  assert.equal(await page.locator('[data-left]').count(), 0, 'no dead end')
  assert.equal(await page.getByRole('button', { name: 'Try again' }).count(), 0)
}
