import assert from 'node:assert/strict'

export default async function (page, { base, scenario, observeIssue }) {
  await scenario('close-issue')
  await observeIssue('1:620', { delivery_approved: true, closable: false })
  let reads = 0
  const writes = []
  const preview = {
    revision: 'current',
    delivery: {
      unit_id: 'delivery', member_count: 2, unit_paused: false, pending_decision: false,
      members: [{ member_id: 'first', issue_id: '1:620', number: 620 }, { member_id: 'second', issue_id: '1:621', number: 621 }],
      dependent_members: ['second'],
      decision: { member_id: 'first', membership_generation: 3, dependent_decisions: { second: 'wait_external' } },
    },
  }
  await page.route('**/api/issues/delivery?*', async route => {
    reads += 1
    await route.fulfill({ json: { ok: true, ...preview } })
  })
  await page.route('**/api/issues/delivery', async route => {
    writes.push(route.request().postDataJSON())
    await route.fulfill({ json: { ok: true, request: 'accepted', status: 'pending' } })
  })
  await page.goto(base + '/issues/1:620')
  await page.getByRole('button', { name: 'Grouped delivery', exact: true }).waitFor()
  assert.equal(reads, 0, 'membership is loaded only when opened')
  assert.equal(await page.getByRole('button', { name: 'Close issue…', exact: true }).count(), 0)
  await page.getByRole('button', { name: 'Grouped delivery', exact: true }).click()
  await page.getByRole('button', { name: 'Remove from delivery', exact: true }).waitFor()
  assert.equal(reads, 1)
  await page.getByRole('button', { name: 'Grouped delivery', exact: true }).click()
  await page.getByRole('button', { name: 'Grouped delivery', exact: true }).click()
  assert.equal(reads, 1, 'reopening an unchanged revision uses the local preview')
  await page.getByRole('button', { name: 'Close this issue', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: 'Close this issue', exact: true })
  await dialog.waitFor()
  assert.match(await dialog.innerText(), /Existing pull requests stay open/)
  assert.match(await dialog.innerText(), /#621 will wait for this issue as an external prerequisite/)
  assert.equal(await dialog.getByRole('button', { name: 'Close this issue', exact: true }).isDisabled(), true)
  await dialog.getByRole('textbox', { name: 'Decision or reason' }).fill('No longer needed')
  await dialog.getByRole('button', { name: 'Close this issue', exact: true }).click()
  await dialog.waitFor({ state: 'detached' })
  assert.equal(writes.length, 1)
  assert.deepEqual(writes[0].membership, preview.delivery.decision)
  assert.equal(writes[0].action, 'close_member')
  assert.equal(writes[0].revision, 'current')
  assert.match(writes[0].request_id, /^[0-9a-f-]{36}$/)
}
