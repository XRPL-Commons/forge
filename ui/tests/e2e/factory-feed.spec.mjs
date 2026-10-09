import assert from 'node:assert/strict'
export default async function(page, { base, scenario, requests }) {
  await scenario('default')
  await page.goto(base + '/issues/1%3A610')
  await page.locator('#issue-title').waitFor()
  await page.waitForFunction(() => document.querySelector('#__nuxt')?.__vue_app__?.config.globalProperties.$nuxt?.isHydrating === false)
  await page.waitForTimeout(1200)
  const reads = (await requests()).filter(row => row.method === 'GET')
  assert.ok(reads.some(row => row.path === '/api/factory/changes'), 'header and detail share a live feed')
  assert.equal(reads.filter(row => row.path === '/api/factory' && row.query.issue).length, 0, 'successful detail carries its jobs without duplicate factory reads')
  const tails = reads.filter(row => row.path === '/api/factory/output')
  assert.ok(tails.length > 0 && tails.every(row => row.query.id === 'attempt'), 'the activity bubble reads only the active job tail')
  assert.equal(reads.filter(row => row.path === '/api/factory' && row.query.section === 'board').length, 1, 'header and detail share the cached initial board')
}
