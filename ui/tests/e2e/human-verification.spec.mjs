import assert from 'node:assert/strict'

export default async function (page, { base, scenario, observeIssue }) {
  await scenario('approval')
  const what = 'Build the release image. POST synthetic input to /api/export-pdf. Confirm %PDF-.'
  const why = 'Workers have no Docker socket.'
  let automatic = false
  let explicit = false
  let gate = 'verification-current-candidate'
  let revision = 'verification-revision-1'
  let confirmed = false
  const observe = () => observeIssue('1:614', {
    revision,
    verification_request: confirmed ? null : { what, why, created_at: 1,
      ...(explicit ? { gate_id: gate, can_confirm: true } : {}),
      ci: automatic ? [{ name: 'release-smoke', app_id: 123 }] : [] },
    approval: automatic ? null : { pr: 30, reason: what },
    actions: automatic ? ['retry'] : ['assign_reviewer', 'retry'],
  })
  await observe()
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
  await observe()
  await page.waitForFunction(() => document.querySelector('#issue-verification')?.textContent.includes('release-smoke'))
  assert.equal(await page.getByRole('button', { name: 'Approve…', exact: true }).count(), 0)
  assert.match(await panel.innerText(), /Forge checks them automatically/)

  automatic = false
  explicit = true
  await observe()
  const submissions = []
  await page.route('**/api/issues/verify', async route => {
    const payload = route.request().postDataJSON()
    submissions.push(payload)
    if (submissions.length === 1) {
      revision = 'verification-revision-2'
      await observe()
      await route.fulfill({ json: { ok: false, reason: 'Issue changed; reload before verifying' } })
    } else {
      confirmed = true
      await observe()
      await route.fulfill({ json: { ok: true, receipt: { candidate_id: 'candidate-current' } } })
    }
  })
  const confirm = panel.getByRole('button', { name: 'Confirm verification passed' })
  await confirm.waitFor()
  assert.equal(await confirm.isDisabled(), true)
  await panel.getByRole('textbox', { name: 'Verification evidence' }).fill('Ran the release image and confirmed a PDF response.')
  const revised = page.waitForResponse(async response => response.url().includes('/api/issues?issue=')
    && (await response.json()).issue?.revision === 'verification-revision-2', { timeout: 10000 })
  await confirm.click()
  await panel.getByRole('alert').getByText('Issue changed; reload before verifying').waitFor()
  assert.equal(submissions[0].gate_id, gate)
  assert.equal(submissions[0].revision, 'verification-revision-1')
  assert.equal(submissions[0].statement, 'Ran the release image and confirmed a PDF response.')
  assert.match(submissions[0].request_id, /^[0-9a-f-]{36}$/)
  // Refresh changes revision without changing the exact candidate's gate.
  await revised
  await confirm.click()
  await panel.waitFor({ state: 'hidden' })
  assert.equal(submissions[1].revision, 'verification-revision-2')
  assert.notEqual(submissions[1].request_id, submissions[0].request_id)

  // A replacement candidate requires a new statement; old text is not carried
  // into the next candidate's confirmation.
  confirmed = false
  await observe()
  await panel.waitFor()
  await panel.getByRole('textbox', { name: 'Verification evidence' }).fill('Old candidate check')
  gate = 'verification-replacement-candidate'
  await observe()
  await panel.getByRole('alert').getByText('The verification request changed.', { exact: false }).waitFor()
  assert.equal(await panel.getByRole('textbox', { name: 'Verification evidence' }).inputValue(), '')
  assert.equal(await confirm.isDisabled(), true)

}
