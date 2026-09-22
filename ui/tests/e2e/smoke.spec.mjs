import assert from 'node:assert/strict'

// End-to-end smoke test: the app loads, basic navigation works, and the
// Nitro /api proxy (Task 2) behaves correctly against the real browser and
// the mock Forge API — same-origin action guard, upstream forwarding,
// upstream-down handling, and cookie pass-through on the GitHub OAuth
// callback.
export default async function (page, { base, scenario, requests }) {
  await page.goto(base + '/')
  assert.equal(await page.title(), 'Activity · Forge')
  await page.getByRole('link', { name: 'Issues' }).click()
  await page.waitForURL(base + '/issues')
  const status = await page.evaluate(async () => (await fetch('/api/factory')).status)
  assert.equal(status, 200)

  // A POST without X-Forge-Action is rejected by the Nitro proxy itself
  // (403), and never reaches the mock.
  const before = await requests()
  const rejected = await page.evaluate(async () => {
    const res = await fetch('/api/factory', { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' })
    return res.status
  })
  assert.equal(rejected, 403)
  const afterRejected = await requests()
  assert.equal(afterRejected.length, before.length, 'a 403-rejected POST must not reach the mock')

  // A POST with X-Forge-Action reaches the mock and is recorded there.
  const accepted = await page.evaluate(async () => {
    const res = await fetch('/api/factory', {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'X-Forge-Action': 'synthetic' },
      body: '{}',
    })
    return res.status
  })
  assert.equal(accepted, 200)
  const afterAccepted = await requests()
  assert.equal(afterAccepted.length, afterRejected.length + 1)
  const last = afterAccepted[afterAccepted.length - 1]
  assert.equal(last.method, 'POST')
  assert.equal(last.path, '/api/factory')

  // With the mock upstream unreachable, the proxy answers 502 with its
  // exact reason string.
  await scenario('down')
  const down = await page.evaluate(async () => {
    const res = await fetch('/api/factory')
    return { status: res.status, body: await res.json() }
  })
  assert.equal(down.status, 502)
  assert.equal(down.body.reason, 'Forge status service is unavailable. Check forge-status on the server.')
  await scenario('default')

  // A Set-Cookie returned by the mock on the GitHub OAuth callback reaches
  // the browser through the proxy's cookie pass-through.
  await page.evaluate(async () => { await fetch('/onboarding/github/callback') })
  const cookies = await page.context().cookies(base)
  const setupCookie = cookies.find((c) => c.name === 'forge_setup')
  assert.ok(setupCookie, 'forge_setup cookie was not set')
  assert.equal(setupCookie.value, 'synthetic-cookie-Kf3n0pQ7rLxW9sVbZt2yHdE')
  assert.equal(setupCookie.httpOnly, true)
  assert.equal(setupCookie.path, '/')
}
