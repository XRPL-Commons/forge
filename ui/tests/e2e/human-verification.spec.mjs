import assert from 'node:assert/strict'

export default async function (page, { base, scenario }) {
  await scenario('approval')
  const what = 'Build the release image. POST synthetic input to /api/export-pdf. Confirm %PDF-.'
  const why = 'Workers have no Docker socket.'
  let automatic = false
  await page.route('**/api/issues?issue=*', async route => {
    const response = await route.fetch({ headers: { ...route.request().headers(), 'if-none-match': '' } })
    const body = await response.json()
    body.issue.verification_request = { what, why, created_at: 1,
      ci: automatic ? [{ name: 'release-smoke', app_id: 123 }] : [] }
    body.issue.approval = automatic ? null : { pr: 30, reason: what }
    body.issue.actions = automatic ? ['retry'] : ['assign_reviewer', 'retry']
    await route.fulfill({ response, json: body })
  })
  await page.goto(base + '/issues/1%3A614')
  const panel = page.locator('#issue-verification')
  await panel.waitFor()
  assert.match(await panel.innerText(), /Workers have no Docker socket/)
  assert.ok((await panel.innerText()).includes(what))
  await page.getByRole('button', { name: 'Approve…', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByText('Run the requested check before approving.', { exact: false }).waitFor()
  assert.ok((await dialog.innerText()).includes(what))
  const link = dialog.getByRole('link', { name: 'Review and approve on GitHub' })
  assert.match(await link.getAttribute('href'), /github\.com\/.+\/pull\/30/)
  await page.keyboard.press('Escape')
  await page.getByRole('button', { name: 'Retry attempt', exact: true }).click()
  await dialog.getByRole('button', { name: 'Retry attempt', exact: true }).waitFor()
  assert.equal(await dialog.getByRole('button', { name: 'Retry attempt', exact: true }).isDisabled(), true)
  await page.keyboard.press('Escape')

  automatic = true
  await page.waitForFunction(() => document.querySelector('#issue-verification')?.textContent.includes('release-smoke'))
  assert.equal(await page.getByRole('button', { name: 'Approve…', exact: true }).count(), 0)
  assert.match(await panel.innerText(), /Forge checks them automatically/)
}
