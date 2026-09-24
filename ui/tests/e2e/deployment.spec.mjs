import assert from 'node:assert/strict'

function waitForText(page, text) {
  return page.waitForFunction((needle) => document.body.innerText.includes(needle), text, { timeout: 10000 })
}

async function waitForRequests(requests, match, timeout = 10000) {
  const deadline = Date.now() + timeout
  for (;;) {
    const found = (await requests()).filter(match)
    if (found.length) return found
    if (Date.now() > deadline) throw new Error('timed out waiting for a request: ' + match.toString())
    await new Promise((resolve) => setTimeout(resolve, 50))
  }
}

const posted = (path) => (entry) => entry.method === 'POST' && entry.path === path

// End-to-end behaviour of deployment tracking: the Deployment card on
// Settings · Repository setup (the strategist's proposal, the decision it
// posts, and a confirmed profile with its observation), the Activity board's
// deployed box, and the issue page's Deployment line.
export default async function (page, { base, scenario, requests }) {
  await scenario('deployment')

  // Proposed: what the strategist read, with the domain it found prefilled
  // and nothing saved yet.
  await page.goto(base + '/settings/repository?repo_id=1')
  await waitForText(page, 'Proposal ready')
  assert.equal(
    (await page.locator('#deployment-sentence').innerText()).trim(),
    'Deploys with GitHub Actions and publishes its commit on /health.',
  )
  assert.deepEqual(
    (await page.locator('#deployment-facts li').allInnerTexts()).map((line) => line.trim()),
    ['Strategy · Health endpoint', 'Deploy · .github/workflows/deploy.yml → production'],
  )
  assert.equal((await page.locator('#deployment-exposure').innerText()).trim(), 'Commit read from /health `commit`')
  assert.equal(await page.locator('#deployment-domain').inputValue(), 'academy.xrpl-commons.org')

  // Confirm posts exactly the repository, the revision, the decision and the
  // domain the operator is looking at.
  await page.getByRole('button', { name: 'Confirm' }).click()
  const confirms = await waitForRequests(requests, posted('/api/repositories/deployment'))
  assert.equal(confirms.length, 1)
  assert.deepEqual(confirms[0].body, {
    repo_id: 1, revision: 'deploy-rev-1', action: 'confirm', domain: 'academy.xrpl-commons.org',
  })
  // The response is the new snapshot, so the card settles without a poll.
  await waitForText(page, 'Tracked')
  assert.equal(await page.locator('#deployment-domain').count(), 0)

  // Confirmed: the profile as one line, and what Forge last observed.
  await page.goto(base + '/settings/repository?repo_id=2')
  await waitForText(page, 'Tracked')
  assert.equal(
    (await page.locator('#deployment-facts li').innerText()).trim(),
    'Response header · workflow .github/workflows/publish.yml · environment production · hackathon.xrpl-commons.org',
  )
  assert.equal((await page.locator('#deployment-exposure').innerText()).trim(), 'Commit read from header `X-Build-Commit`')
  await page.waitForFunction(
    () => (document.getElementById('deployment-observation')?.innerText ?? '').startsWith('live commit abc1234 · deploy succeeded '),
    null,
    { timeout: 10000 },
  )

  // The card holds up on a phone: nothing spills sideways at 390 px.
  await page.setViewportSize({ width: 390, height: 800 })
  await page.waitForFunction(
    () => document.documentElement.scrollWidth <= document.documentElement.clientWidth,
    null,
    { timeout: 5000 },
  )
  await page.setViewportSize({ width: 1280, height: 800 })

  // GitHub access lists the recommended permission without failing the row.
  await page.goto(base + '/settings/github')
  await waitForText(page, 'Recommended deployments: read')
  const controller = page.locator('#connections [data-key="controller"]')
  assert.equal((await controller.locator('[data-badge]').innerText()).trim(), 'Connected')

  // Activity: the merged box becomes the deployed box, and says what is still
  // waiting behind it.
  await page.goto(base + '/')
  const box = page.locator('[data-breakdown="merged"]')
  await page.waitForFunction(
    () => document.querySelector('[data-breakdown="merged"]')?.innerText.includes('Deployed today'),
    null,
    { timeout: 10000 },
  )
  // Only the tracked repository's merge with no verdict is waiting: the
  // failed one is counted beside it, the untracked repository's is not counted.
  assert.equal(await box.getAttribute('title'), '1 merged and waiting · 1 deploy failed')

  // The completed card carries the same verdict in one word.
  await page.locator('#completed summary').click()
  await waitForText(page, 'deployed')

  // Issue page: one Deployment line, in the words of the verdict.
  await page.goto(base + '/issues/2%3A606')
  await waitForText(page, 'Normalize webhook signature verification')
  const line = (await page.locator('#issue-deployment').innerText()).trim()
  assert.ok(line.startsWith('Deployment · Deployed · commit abc1234 read from the live site'), line)
}
