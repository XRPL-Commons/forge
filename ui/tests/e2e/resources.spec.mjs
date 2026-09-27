import assert from 'node:assert/strict'
import { writeFile } from 'node:fs/promises'
export default async function(page, { base, requests }) {
  // Delayed client API calls make accidental navigation round trips observable.
  await page.route('**/api/**', async route => {
    if (route.request().method() === 'GET' && !route.request().url().includes('_nuxt_icon')) await new Promise(r => setTimeout(r, 750))
    await route.continue()
  })
  // The description render is a separate round trip; a warm detail must not wait for it either.
  await page.route('**/_ui/markdown', async route => { await new Promise(r => setTimeout(r, 750)); await route.continue() })
  await page.goto(base + '/issues')
  await page.locator('[data-key="1:610"] a').first().waitFor()
  await page.locator('[data-key="1:610"] a').first().click()
  await page.locator('#issue-title').waitFor()
  await page.getByRole('link', { name: 'Issues', exact: true }).click()
  await page.locator('[data-key="1:610"] a').first().waitFor()
  const elapsed = await page.evaluate(async () => {
    const started = performance.now()
    const done = new Promise(resolve => {
      const observer = new MutationObserver(() => {
        if (document.querySelector('#issue-title')) { observer.disconnect(); requestAnimationFrame(() => resolve(performance.now() - started)) }
      })
      observer.observe(document.body, { childList: true, subtree: true })
    })
    document.querySelector('[data-key="1:610"] a').click()
    return done
  })
  if (!process.env.FORGE_BENCH_BASELINE) assert.ok(elapsed < 100, `warm detail paints within 100ms despite 750ms API delay (${elapsed.toFixed(1)}ms)`)
  await page.locator('.forge-markdown', { hasText: 'Course navigation update' }).waitFor()
  const reads = (await requests()).filter(r => r.method === 'GET')
  const report = { clientApiDelayMs: 750, warmIssueRenderMs: elapsed, requests: {
    factory: reads.filter(r => r.path === '/api/factory').length,
    issue: reads.filter(r => r.path === '/api/issues' && r.query.issue).length,
    list: reads.filter(r => r.path === '/api/issues' && !r.query.issue).length } }
  await writeFile(process.env.FORGE_BENCH_BASELINE ? '/tmp/forge-ui-resource-baseline.json' : '/tmp/forge-ui-resource-measurements.json', JSON.stringify(report, null, 2))
  console.log('resource measurements', report)
}
