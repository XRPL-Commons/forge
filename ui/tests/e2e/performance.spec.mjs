import assert from 'node:assert/strict'

export default async function(page, { base, requests }) {
  for (const [path, marker] of [
    ['/', 'id="columns"'], ['/issues', 'data-key="1:610"'], ['/issues/1:610', 'id="issue-title"'],
    ['/settings', 'id="capacity-build"'], ['/settings/repositories', 'data-key="1"'],
    ['/settings/github', 'data-key="build"'],
  ]) {
    const response = await page.request.get(base + path)
    assert.equal(response.status(), 200)
    assert.ok((await response.text()).includes(marker), `${path} includes its first view in server HTML`)
  }
  const unchanged = []
  page.on('response', response => { if (response.status() === 304) unchanged.push(response.url()) })
  await page.goto(base + '/')
  await page.waitForFunction(() => document.querySelector('#__nuxt')?.__vue_app__?.config.globalProperties.$nuxt?.isHydrating === false)
  let reads = await requests()
  assert.equal(reads.filter(r => r.path === '/api/factory' && r.query.section === 'completed').length, 0)
  assert.equal(reads.filter(r => r.path === '/api/factory/output').length, 0)
  await page.locator('#completed summary').click()
  await page.waitForFunction(() => document.querySelector('#completed .issue-card'))
  reads = await requests()
  assert.ok(reads.some(r => r.path === '/api/factory' && r.query.section === 'completed'))
  await page.waitForTimeout(1000)
  assert.ok(unchanged.some(url => url.includes('/api/factory')), 'unchanged polls use bodyless 304 responses')
  await page.locator('#completed summary').click()
  await page.waitForTimeout(100)
  const before = (await requests()).filter(r => r.path === '/api/factory' && r.query.section === 'completed').length
  await page.waitForTimeout(800)
  reads = await requests()
  const after = reads.filter(r => r.path === '/api/factory' && r.query.section === 'completed').length
  assert.equal(after, before, 'collapsed sections stop fetching')
}
