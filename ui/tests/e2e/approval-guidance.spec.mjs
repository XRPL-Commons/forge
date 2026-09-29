import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

export default async function (page, { base, scenario }) {
  await scenario('approval')
  const fixture = JSON.parse(await readFile(new URL('./fixtures/approval/issues.issue.json', import.meta.url)))
  let resolved = false
  await page.route('**/api/issues?*', async route => {
    const response = await route.fetch({ headers: { ...route.request().headers(), 'if-none-match': '' } })
    const body = await response.json()
    if (new URL(route.request().url()).searchParams.has('issue')) {
      if (resolved) {
        body.issue.approval = null
        body.issue.actions = []
        body.issue.status = 'completed'
      }
    } else {
      body.issues = [fixture.issue]
      body.total = 1
    }
    await route.fulfill({ response, json: body })
  })
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
  resolved = true
  await page.waitForFunction(() => !document.body.innerText.includes('Waiting for your approval on GitHub'))
  assert.equal(await page.getByRole('button', { name: 'Approve…', exact: true }).count(), 0)

  await page.unroute('**/api/issues?*')
  await page.route('**/api/issues?issue=*', async route => {
    const response = await route.fetch({ headers: { ...route.request().headers(), 'if-none-match': '' } })
    const body = await response.json()
    body.issue.approval = null
    body.issue.actions = []
    body.issue.merge_blocker = { reason: 'GitHub refused to merge: branch rules still block it' }
    body.issue.summary = { headline: 'The merge is blocked', plain: body.issue.merge_blocker.reason }
    await route.fulfill({ response, json: body })
  })
  await page.goto(base + '/issues/1%3A614?approve=1')
  await page.getByText('The merge is blocked', { exact: true }).waitFor()
  assert.equal(await page.getByRole('button', { name: 'Approve…', exact: true }).count(), 0)
  assert.equal(await page.getByRole('dialog').count(), 0)
  assert.match(await page.locator('body').innerText(), /pull-request bypass/)
  assert.equal(await page.getByRole('link', { name: 'Repository setup →' }).count(), 1)
}
