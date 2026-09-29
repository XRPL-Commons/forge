import { chromium } from 'playwright-core'
import { writeFile } from 'node:fs/promises'
const base = process.env.FORGE_BENCH_URL || 'http://127.0.0.1:17004'
const browser = await chromium.launch({ headless: true, chromiumSandbox: false, ...(process.env.FORGE_BROWSER_PATH ? { executablePath: process.env.FORGE_BROWSER_PATH } : {}) })
try {
  const context = await browser.newContext()
  const listResponse = await context.request.get(base + '/api/issues?state=open&page=1')
  const list = await listResponse.json()
  const key = list.issues?.[0]?.key
  if (!key) throw new Error('Need one visible issue to measure detail')
  const cases = [
    ['activity', '/', '#columns', '/api/factory'],
    ['issues', '/issues', '[data-key]', '/api/issues?state=open&page=1'],
    ['detail', '/issues/' + encodeURIComponent(key), '#issue-title', '/api/issues?issue=' + encodeURIComponent(key)],
    ['settings', '/settings', '#capacity-build', '/api/settings'],
    ['repositories', '/settings/repositories', '[data-key]', '/api/settings'],
    ['github', '/settings/github', '#connections [data-key]', '/api/settings'],
  ]
  const report = { base, samples: [] }
  for (const [name, path, selector, api] of cases) {
    const page = await context.newPage()
    await page.addInitScript(selector => {
      const observer = new MutationObserver(() => {
        if (document.querySelector(selector) && !window.firstViewMs) {
          window.firstViewMs = performance.now(); observer.disconnect()
        }
      }); observer.observe(document, { childList: true, subtree: true })
    }, selector)
    const response = await page.goto(base + path, { waitUntil: 'domcontentloaded' })
    await page.locator(selector).first().waitFor({ state: 'attached', timeout: 30000 })
    const html = await response.text()
    const metrics = await page.evaluate(() => ({ firstViewMs: window.firstViewMs, ttfbMs: performance.getEntriesByType('navigation')[0].responseStart }))
    const measuredApi = process.env.FORGE_BENCH_OPTIMIZED && api === '/api/factory' ? api + '?section=board'
      : process.env.FORGE_BENCH_OPTIMIZED && api === '/api/settings' ? api + '?summary=1' : api
    const payload = await context.request.get(base + measuredApi)
    const etag = payload.headers().etag
    const unchanged = etag ? await context.request.get(base + measuredApi, { headers: { 'If-None-Match': etag } }) : null
    report.samples.push({ name, ...metrics, htmlBytes: Buffer.byteLength(html), apiBytes: (await payload.body()).length,
      unchangedStatus: unchanged?.status(), unchangedBytes: unchanged ? (await unchanged.body()).length : null,
      ssrHasData: name === 'detail' ? html.includes('id="issue-title"') : name === 'activity' ? html.includes('id="columns"') : name === 'settings' ? html.includes('id="capacity-build"') : html.includes('data-key=') })
    console.log(name, report.samples.at(-1))
    await page.close()
  }
  console.log(JSON.stringify(report, null, 2))
  if (process.env.FORGE_BENCH_OUTPUT) await writeFile(process.env.FORGE_BENCH_OUTPUT, JSON.stringify(report, null, 2))
  await context.close()
} finally { await browser.close() }
