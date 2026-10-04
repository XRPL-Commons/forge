import assert from 'node:assert/strict'

export default async function (page, { base, scenario, requests }) {
  const compressed = await page.request.get(base + '/api/factory', { headers: { 'accept-encoding': 'gzip' } })
  assert.equal(compressed.headers()['content-encoding'], 'gzip')
  assert.match(compressed.headers().vary, /Accept-Encoding/i)
  assert.equal((await compressed.json()).ok, true)
  const plain = await page.request.get(base + '/api/factory', { headers: { 'accept-encoding': 'gzip;q=0' } })
  assert.equal(plain.headers()['content-encoding'], undefined)
  const unchanged = await page.request.get(base + '/api/factory', { headers: { 'accept-encoding': 'gzip', 'if-none-match': compressed.headers().etag } })
  assert.equal(unchanged.status(), 304)
  assert.equal(unchanged.headers()['content-encoding'], undefined)
  assert.equal((await unchanged.body()).length, 0)

  await scenario('approval')
  await page.goto(base + '/issues/1%3A614')
  await page.getByRole('button', { name: 'Check now' }).click()
  await page.getByText('Checking GitHub…', { exact: false }).waitFor()
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { value: true, configurable: true })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  // Let requests already in flight finish, then cross the four-second timer.
  await page.waitForTimeout(500)
  const reads = async () => (await requests()).filter(r => r.method === 'GET' && r.path === '/api/issues').length
  const before = await reads()
  await page.waitForTimeout(4500)
  assert.equal(await reads(), before, 'Check now must not poll a hidden tab')
  const resumed = page.waitForResponse(response => response.url().includes('/api/issues?issue='))
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { value: false, configurable: true })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await resumed
  assert.ok(await reads() > before, 'visible tab refreshes immediately')
}
