import assert from 'node:assert/strict'

export default async function(page, { base, requests }) {
  for (const [path, marker] of [
    ['/', 'id="factory-line"'], ['/issues', 'data-key="1:610"'], ['/issues/1:610', 'id="issue-title"'],
    ['/settings', 'id="capacity-build"'], ['/settings/repositories', 'data-key="1"'],
    ['/settings/github', 'data-key="build"'],
  ]) {
    const response = await page.request.get(base + path)
    assert.equal(response.status(), 200)
    const html = await response.text()
    assert.ok(html.includes(marker), `${path} includes its first view in server HTML`)
    // Recently completed is fetched by the browser, a page at a time, never
    // rendered into the first paint.
    if (path === '/') assert.ok(!html.includes('id="completed-end"') && !/id="completed"[^]*class="issue-card/.test(html), 'no completed cards in server HTML')
  }
  const unchanged = []
  page.on('response', response => { if (response.status() === 304) unchanged.push(response.url()) })
  await page.goto(base + '/')
  await page.waitForFunction(() => document.querySelector('#__nuxt')?.__vue_app__?.config.globalProperties.$nuxt?.isHydrating === false)
  let reads = await requests()
  assert.equal(reads.filter(r => r.path === '/api/factory/output').length, 0)
  assert.equal(reads.filter(r => r.path === '/api/factory' && r.query.section === 'runners').length, 0, 'folded investigations are not fetched')
  await page.waitForFunction(() => document.querySelector('#completed .issue-card'))
  reads = await requests()
  const completed = reads.filter(r => r.path === '/api/factory' && r.query.section === 'completed')
  assert.ok(completed.length >= 1)
  assert.ok(completed.every(r => r.query.limit === '10'), 'completed issues are only ever read a page at a time')
  if (!unchanged.some(url => url.includes('/api/factory'))) {
    await page.waitForResponse(response => response.status() === 304 && new URL(response.url()).pathname === '/api/factory', { timeout: 10000 })
  }
  assert.ok(unchanged.some(url => url.includes('/api/factory')), 'unchanged polls use bodyless 304 responses')
  // The pages are read once each, not polled.
  const settled = (await requests()).filter(r => r.path === '/api/factory' && r.query.section === 'completed').length
  await page.waitForTimeout(800)
  reads = await requests()
  assert.equal(reads.filter(r => r.path === '/api/factory' && r.query.section === 'completed').length, settled, 'completed pages are not polled')
}
