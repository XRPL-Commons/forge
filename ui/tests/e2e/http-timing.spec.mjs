import assert from 'node:assert/strict'

export default async function(page, { base, requests }) {
  const samples = []
  const browserBoards = []
  page.on('console', message => {
    if (message.text().startsWith('[forge-http-timing] ')) samples.push(JSON.parse(message.text().slice('[forge-http-timing] '.length)))
  })
  page.on('request', request => {
    const url = new URL(request.url())
    if (url.pathname === '/api/factory' && url.searchParams.get('section') === 'board') browserBoards.push(request)
  })
  await page.goto(base + '/?timings=1')
  await page.locator('#factory-line').waitFor()
  await page.waitForFunction(() => document.querySelector('#__nuxt')?.__vue_app__?.config.globalProperties.$nuxt?.isHydrating === false)
  assert.equal(samples.filter(row => row.source === 'server_render' && row.view === 'board').length, 1,
    'the actual SSR board fetch is hydrated and emitted once')
  assert.equal(browserBoards.length, 0, 'collecting the SSR timing does not refetch the board in the browser')
  assert.equal((await requests()).filter(row => row.path === '/api/factory' && row.query.section === 'board').length, 1)

  await page.locator('#completed').scrollIntoViewIfNeeded()
  await page.locator('#completed .issue-card').first().waitFor()
  assert.ok(samples.some(row => row.source === 'browser' && row.view === 'completed'))
  for (const sample of samples) {
    assert.deepEqual(Object.keys(sample).sort(), ['fetchMs', 'serverMs', 'source', 'status', 'view'])
    assert.ok(Number.isFinite(sample.fetchMs) && sample.fetchMs >= 0)
    assert.equal(typeof sample.status, 'number')
    assert.ok(Object.values(sample.serverMs).every(Number.isFinite))
    assert.ok(['server_render', 'browser'].includes(sample.source))
  }

  // A separate document on the same running Nuxt server must not inherit the
  // opted-in request's samples or enable flag.
  const other = await page.context().newPage()
  const otherSamples = []
  other.on('console', message => { if (message.text().startsWith('[forge-http-timing] ')) otherSamples.push(message.text()) })
  try {
    await other.goto(base + '/')
    await other.locator('#factory-line').waitFor()
    await other.waitForFunction(() => document.querySelector('#__nuxt')?.__vue_app__?.config.globalProperties.$nuxt?.isHydrating === false)
    assert.deepEqual(otherSamples, [])
  } finally { await other.close() }
  samples.length = 0
  await page.reload()
  await page.waitForFunction(() => document.querySelector('#__nuxt')?.__vue_app__?.config.globalProperties.$nuxt?.isHydrating === false)
  assert.equal(samples.filter(row => row.source === 'server_render' && row.view === 'board').length, 1,
    'reload creates a fresh request-scoped sample window')
}
