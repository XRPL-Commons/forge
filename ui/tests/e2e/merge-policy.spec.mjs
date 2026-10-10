import assert from 'node:assert/strict'

export default async function (page, { base, scenario, requests, observeIssue }) {
  await page.goto(base + '/settings/repositories/2')
  const policy = page.locator('#repository-merge-policy')
  await policy.waitFor()
  assert.match(await policy.innerText(), /One counted human approval/)
  assert.equal(await policy.locator('button,input,select').count(), 0, 'policy is read-only in Settings')
  assert.equal((await requests()).filter(row => row.method === 'POST' && row.path.includes('policy')).length, 0)

  await scenario('approval')
  await observeIssue('1:614', { merge_policy: 'auto' })
  await page.goto(base + '/issues/1%3A614')
  await page.locator('#issue-merge-policy').waitFor()
  assert.equal((await page.locator('#issue-merge-policy').innerText()).trim(),
    'Forge merges this automatically once checks pass. A security audit or a verification may still need your approval.')
  await page.getByRole('button', { name: 'Approve…', exact: true }).waitFor()

  await observeIssue('1:614', { merge_policy: 'human' })
  await page.waitForFunction(() => document.querySelector('#issue-merge-policy')?.textContent.includes('Forge merges this only after a person approves it.'))
}
