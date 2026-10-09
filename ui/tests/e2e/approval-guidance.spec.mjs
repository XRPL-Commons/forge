import assert from 'node:assert/strict'

export default async function (page, { base, scenario, observeIssue }) {
  await scenario('approval')
  await observeIssue('1:614', {}, { list: true })
  await page.goto(base + '/issues')
  await page.getByRole('link', { name: 'Approve…', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: 'Approve the pull request' })
  await dialog.waitFor()
  assert.match(await dialog.innerText(), /A person must sign off on the key rotation/)
  assert.match(await dialog.innerText(), /Forge never approves on your behalf/)
  assert.match(await dialog.innerText(), /Forge merges automatically/)
  const link = dialog.getByRole('link', { name: 'Review and approve on GitHub' })
  assert.equal(await link.getAttribute('href'), 'https://github.com/XRPL-Commons/academy/pull/31/files')
  assert.equal(await link.getAttribute('target'), '_blank')
  assert.match(await link.getAttribute('rel'), /noopener/)
  // Asking another reviewer is still available inside and outside the modal.
  await dialog.getByRole('button', { name: 'Assign reviewer…', exact: true }).click()
  await page.getByRole('dialog', { name: 'Assign reviewer', exact: true }).waitFor()
  await page.keyboard.press('Escape')
  await page.locator('#issue-actions').getByRole('button', { name: 'Approve…', exact: true }).click()
  await dialog.waitFor()
  await page.context().route('https://github.com/**', route => route.fulfill({ body: 'GitHub review page' }))
  const popupPromise = page.waitForEvent('popup')
  await link.click()
  const popup = await popupPromise
  await popup.close()
  await page.waitForFunction(() => document.body.innerText.includes('Waiting for your approval on GitHub'))
  assert.equal(await dialog.count(), 0)
  await observeIssue('1:614', { approval: null, actions: [], status: 'completed' })
  await page.waitForFunction(() => !document.body.innerText.includes('Waiting for your approval on GitHub'))
  assert.equal(await page.getByRole('button', { name: 'Approve…', exact: true }).count(), 0)

  await observeIssue('1:614', { approval: null, actions: [],
    merge_blocker: { reason: 'GitHub refused to merge: branch rules still block it' },
    summary: { headline: 'The merge is blocked', plain: 'GitHub refused to merge: branch rules still block it' } })
  await page.goto(base + '/issues/1%3A614?approve=1')
  await page.getByText('The merge is blocked', { exact: true }).waitFor()
  assert.equal(await page.getByRole('button', { name: 'Approve…', exact: true }).count(), 0)
  assert.equal(await page.getByRole('dialog').count(), 0)
  assert.match(await page.locator('body').innerText(), /pull-request bypass/)
  assert.equal(await page.getByRole('link', { name: 'Repository setup →' }).count(), 1)
}
