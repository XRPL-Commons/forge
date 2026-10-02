import assert from 'node:assert/strict'

// The environment is not the blocker in these issues, so it waits in a
// collapsed section under the next step.
async function openEnvironment(page) {
  const toggle = page.locator('#issue-environment-section').getByRole('button', { name: 'Worker environment' })
  await toggle.waitFor()
  assert.equal(await page.locator('#issue-environment').count(), 0, 'the worker environment starts collapsed')
  await page.waitForFunction(() => document.querySelector('#__nuxt')?.__vue_app__?.config.globalProperties.$nuxt?.isHydrating === false)
  await toggle.click()
  await page.locator('#issue-environment').waitFor()
  // Let the section finish opening, so the select's menu anchors to where it stays.
  await page.waitForFunction(() => document.getAnimations().every(animation => animation.playState !== 'running'))
}

export default async function (page, { base, scenario, requests }) {
  await scenario('issue-environment')
  await page.goto(base + '/issues/1:610')
  await openEnvironment(page)
  const card = page.locator('#issue-environment')
  await card.getByRole('button', { name: 'Change environment' }).waitFor()
  assert.ok((await card.innerText()).includes('MongoDB'))
  assert.ok((await card.innerText()).includes('repository scan found these databases'))
  await card.getByRole('button', { name: 'Change environment' }).click()
  await card.getByRole('combobox').click()
  await page.getByRole('option', { name: 'Choose databases', exact: true }).click()
  await card.getByRole('checkbox', { name: 'Redis', exact: true }).check()
  await card.getByRole('button', { name: 'Save for this issue' }).click()
  await page.waitForFunction(() => document.body.innerText.includes('Environment preference submitted'))
  const saved = (await requests()).filter(r => r.method === 'POST' && r.path === '/api/issues/environment')
  assert.equal(saved.length, 1)
  assert.deepEqual(saved[0].body, { key: '1:610', revision: 'current', environment: { mode: 'full', services: ['mongodb', 'redis'] } })

  await scenario('issue-environment-standard')
  await page.goto(base + '/issues/1:610')
  await openEnvironment(page)
  await card.getByRole('button', { name: 'Change environment' }).click()
  await card.getByRole('combobox').click()
  await page.getByRole('option', { name: 'Automatic', exact: true }).click()
  await card.getByRole('button', { name: 'Save for this issue' }).click()
  await page.waitForFunction(() => document.body.innerText.includes('Environment preference submitted'))
  const reset = (await requests()).filter(r => r.method === 'POST' && r.path === '/api/issues/environment')
  assert.deepEqual(reset[0].body.environment, { mode: 'automatic', services: [] })

  await scenario('issue-environment-busy')
  await page.goto(base + '/issues/1:610')
  await openEnvironment(page)
  await card.waitFor()
  assert.equal(await card.getByRole('button', { name: 'Change environment' }).count(), 0)

  await scenario('issue-environment-conflict')
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(base + '/issues/1:610')
  await openEnvironment(page)
  await card.getByRole('button', { name: 'Change environment' }).click()
  await card.getByRole('button', { name: 'Save for this issue' }).click()
  await card.getByRole('alert').waitFor()
  assert.ok((await card.innerText()).includes('This issue changed'))
  assert.equal(await card.getByRole('button', { name: 'Save for this issue' }).isEnabled(), true)
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
  await card.screenshot({ path: '/tmp/forge-auto-env-mobile.png' })
  await page.setViewportSize({ width: 1440, height: 1000 })
  await card.screenshot({ path: '/tmp/forge-auto-env-desktop.png' })
}
