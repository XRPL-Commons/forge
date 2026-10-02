import assert from 'node:assert/strict'

function waitForText(page, text, timeout = 10000) {
  return page.waitForFunction((needle) => document.body.innerText.includes(needle), text, { timeout })
}

async function waitForRequest(requests, predicate) {
  const until = Date.now() + 10000
  while (Date.now() < until) {
    const found = (await requests()).filter(predicate)
    if (found.length) return found
    await new Promise(resolve => setTimeout(resolve, 100))
  }
  throw new Error('timed out waiting for a recorded request')
}

// The reviews Forge found on GitHub, when it last looked, "Check now", and the
// approver list in Settings · General.
export default async function (page, { base, scenario, requests }) {
  await scenario('approval')
  const now = () => Math.floor(Date.now() / 1000)
  let approvals = [
    { login: 'lucbocahut', state: 'APPROVED', at: now() - 60, counted: true, why: null },
    { login: 'x', state: 'APPROVED', at: null, counted: false, why: 'No write access to the repository' },
  ]
  let lastChecked = now() - 20
  await page.route('**/api/issues?issue=*', async route => {
    const response = await route.fetch({ headers: { ...route.request().headers(), 'if-none-match': '' } })
    const body = await response.json()
    body.issue.approvals = approvals
    body.issue.last_checked = lastChecked
    await route.fulfill({ response, json: body })
  })
  await page.goto(base + '/issues/1%3A614')
  await waitForText(page, '@x approved · not counted: No write access to the repository')
  const list = await page.locator('#issue-approvals').innerText()
  assert.match(list, /@lucbocahut approved · \d{1,2}:\d{2}[^·]* · counted/)
  // Not colour alone: every row carries an icon next to its words.
  assert.equal(await page.locator('#issue-approvals li svg, #issue-approvals li .iconify, #issue-approvals li [class*="i-lucide"]').count(), 2)
  await waitForText(page, 'Checked on GitHub')
  assert.match(await page.locator('#issue-checked').innerText(), /Checked on GitHub \d+ sec ago/)

  // Check now asks for this issue, shows the pending state, and ends once
  // Forge's last GitHub read moves past the click.
  await page.getByRole('button', { name: 'Check now' }).click()
  const asked = await waitForRequest(requests, (entry) => entry.method === 'POST' && entry.path === '/api/issues/refresh')
  assert.deepEqual(asked[0].body, { key: '1:614' })
  await waitForText(page, 'Checking GitHub…')
  approvals = []
  lastChecked = now() + 1
  await page.waitForFunction(() => !document.body.innerText.includes('Checking GitHub…'), null, { timeout: 15000 })
  await waitForText(page, 'No approval found on the pull request yet.')
  assert.equal(await page.locator('#issue-approvals').count(), 0)
  await page.unroute('**/api/issues?issue=*')

  // Settings: the default runtime has an allowlist; switching to "anyone"
  // sends the whole runtime with only human_reviewers emptied.
  await scenario('default')
  let reject = false
  await page.route('**/api/runtime', async route => {
    if (reject) return route.fulfill({ json: { ok: false, reason: 'Runtime settings changed; reload before saving' } })
    const posted = route.request().postDataJSON()
    const response = await route.fetch()
    const body = await response.json()
    body.runtime = { ...body.runtime, human_reviewers: posted.config.human_reviewers }
    await route.fulfill({ response, json: body })
  })
  await page.goto(base + '/settings')
  const logins = page.locator('#approvers-logins')
  await logins.waitFor({ state: 'visible' })
  assert.equal(await logins.inputValue(), 'luc-bocahut')

  await logins.fill('octocat, bad_name')
  await waitForText(page, 'Not a GitHub login: bad_name')
  assert.equal(await page.getByRole('button', { name: 'Save approvers' }).isDisabled(), true)
  await logins.fill('@octocat, hubot')
  await page.getByRole('button', { name: 'Save approvers' }).click()
  await waitForText(page, 'Approver list saved.')
  let posted = (await requests()).filter((entry) => entry.method === 'POST' && entry.path === '/api/runtime')
  assert.equal(posted.length, 1)
  assert.deepEqual(posted[0].body.config.human_reviewers, ['octocat', 'hubot'])
  assert.equal(posted[0].body.config.slots.build, 2)
  assert.equal(posted[0].body.config.image, 'forge/worker:2026.09')
  assert.equal(posted[0].body.revision, '6f2a9c8e1d4b7035af6c2e9d10b4f7a3c5e8d1b6924a7f3c0e5b8a1d4f7c2e9b')
  assert.equal(await logins.inputValue(), 'octocat, hubot')

  await page.getByRole('radio', { name: 'Anyone with write access to the repository' }).click()
  await page.getByRole('button', { name: 'Save approvers' }).click()
  await waitForText(page, 'Approver list saved.')
  posted = (await requests()).filter((entry) => entry.method === 'POST' && entry.path === '/api/runtime')
  assert.equal(posted.length, 2)
  assert.deepEqual(posted[1].body.config.human_reviewers, [])
  assert.equal(posted[1].body.revision, 'runtime-rev-2')
  assert.equal(await logins.count(), 0)

  // A stale revision is reported, and the edit is kept for another try.
  reject = true
  await page.getByRole('radio', { name: 'Only these GitHub users' }).click()
  await logins.fill('octocat')
  await page.getByRole('button', { name: 'Save approvers' }).click()
  await waitForText(page, 'reload before saving')
  assert.equal(await logins.inputValue(), 'octocat')
  await page.unroute('**/api/runtime')
}
