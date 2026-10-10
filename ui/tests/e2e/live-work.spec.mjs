import assert from 'node:assert/strict'
import { moreItems } from './more-menu.mjs'

// A running build, read live: the bubble says what the worker is doing right
// now, the facts say who it is and where the work lands, the output is open
// and follows the tail with heartbeats hidden, and ending the work waits
// behind More. When heartbeats stop, the cube nods off and the bubble says so.
const JOB = '44444444-4444-4444-8444-444444444444'
const SCREENSHOT = process.env.FORGE_E2E_LIVE_SCREENSHOT || ''

function waitForText(page, text) {
  return page.waitForFunction((needle) => document.body.innerText.includes(needle), text, { timeout: 10000 })
}

export default async function (page, { base, scenario, requests }) {
  await scenario('live-work')
  await page.goto(base + '/issues/1%3A610')
  await waitForText(page, 'Make course navigation easier')

  // The bubble: the live activity, quoted, and how long it has run.
  const headline = page.locator('#issue-headline')
  await headline.locator('code').waitFor({ timeout: 10000 })
  assert.equal((await headline.locator('code').innerText()).trim(), 'pnpm build')
  assert.match((await headline.innerText()).trim(), /^Running pnpm build\s+for \d+ min$/)
  assert.match((await page.locator('#issue-liveness').innerText()).trim(), /^alive \d+s ago$/)
  assert.equal(await page.locator('#issue-liveness').getAttribute('data-state'), 'alive')
  assert.equal(await page.locator('#issue-workbench').getAttribute('data-mood'), 'busy')
  // "Working" is the badge: neither the bubble nor the lines above repeat "Building".
  assert.equal(await page.locator('#issue-plain').count(), 0)
  assert.equal(await page.locator('#issue-stage-note').count(), 0)
  assert.equal(await page.locator('#issue-status-reason').count(), 0)

  // Who is working, on which attempt, and where it lands.
  const work = page.locator('#issue-work')
  assert.match((await work.locator('#issue-worker').innerText()).replace(/\s+/g, ' ').trim(), /^Ember · Claude Sonnet 4\.6$/)
  assert.equal((await work.locator('#issue-work-attempt').innerText()).trim(), 'Build, attempt 2 of 3')
  assert.equal((await work.locator('#issue-work-branch').innerText()).trim(), 'forge/610-build')
  assert.ok(await work.locator('#issue-work-time').count())
  // The current stage says how long it has run so far.
  const build = page.locator('#issue-stages [data-stage="build"]')
  assert.equal(await build.getAttribute('aria-current'), 'step')
  assert.match(await build.getAttribute('aria-label'), /^Build: current stage, .+ so far$/)
  assert.equal(await build.locator('[data-took]').count(), 1)

  // One calm line, one question to the worker, and the rest behind More.
  const actions = page.locator('#issue-actions')
  assert.equal((await actions.locator('#issue-ask').innerText()).trim(), 'Ask Ember')
  assert.equal(await actions.getByRole('button', { name: 'Discuss blocker' }).count(), 0)
  assert.deepEqual(await moreItems(page, actions), ['Stop attempt…', 'Close issue…'])

  // The output is open from the start, live, without heartbeat lines; raw
  // shows them on demand.
  const pre = page.locator('#issue-output pre')
  await pre.waitFor()
  assert.match(await page.locator('#issue-output-live').innerText(), /^live · updated \d+s ago$/)
  assert.ok((await pre.innerText()).includes('vite v7 building for production...'))
  assert.equal((await pre.innerText()).includes('"runner"'), false, 'heartbeats are hidden')
  await page.locator('#issue-output-raw').click()
  assert.ok((await pre.innerText()).includes('"runner": "alive"'))
  await page.locator('#issue-output-raw').click()
  assert.equal((await pre.innerText()).includes('"runner"'), false)
  const reads = (await requests()).filter(entry => entry.path === '/api/factory/output')
  assert.ok(reads.length >= 1 && reads.every(entry => entry.query.id === JOB), 'the active job’s output is read')

  // The output section comes first while the worker runs.
  const sections = await page.locator('#issue-sections > section').evaluateAll(nodes => nodes
    .map(node => ({ id: node.id, order: Number(getComputedStyle(node).order) }))
    .sort((left, right) => left.order - right.order).map(node => node.id))
  assert.equal(sections[0], 'issue-output')
  assert.equal(await page.locator('#issue-details').count(), 0, 'no findings, no empty section')

  // Narrow panel: nothing scrolls sideways.
  await page.setViewportSize({ width: 390, height: 900 })
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'no horizontal scroll at 390px')
  if (SCREENSHOT) await page.screenshot({ path: SCREENSHOT, fullPage: true })
  await page.setViewportSize({ width: 1280, height: 900 })

  // Heartbeats stopped: the cube nods off and the bubble says so.
  await page.goto('about:blank')
  await scenario('live-stalled')
  await page.goto(base + '/issues/1%3A610')
  await waitForText(page, 'Ember has gone quiet')
  assert.equal(await page.locator('#issue-liveness').getAttribute('data-state'), 'stalled')
  assert.match((await page.locator('#issue-plain').innerText()).trim(), /^No heartbeat for 5 min\./)
  assert.equal(await page.locator('#issue-workbench').getAttribute('data-mood'), 'sleep')
}
