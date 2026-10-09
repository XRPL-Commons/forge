import assert from 'node:assert/strict'

// End-to-end behaviour of the setup-help page (GET /_ui/help + /setup-help).
// run.mjs starts the app with NITRO_HELP_DOC pointed at
// tests/e2e/fixtures/help.md, a fixed fixture with seven headings — never
// docs/onboarding.md itself — so this spec asserts against that fixture's
// content and never breaks when the real doc is edited.
const FIXTURE_HEADINGS = [
  { id: 'forge-setup-help', text: 'Forge setup help', level: 1 },
  { id: 'host-installation', text: 'Host installation', level: 2 },
  { id: 'github-app-creation', text: 'GitHub App creation', level: 2 },
  { id: 'github-repository-protection', text: 'GitHub repository protection', level: 2 },
  { id: 'coding-provider-accounts', text: 'Coding provider accounts', level: 2 },
  { id: 'verifying-the-setup', text: 'Verifying the setup', level: 2 },
  { id: 'troubleshooting', text: 'Troubleshooting', level: 2 },
]

function waitForText(page, text) {
  return page.waitForFunction((needle) => document.body.innerText.includes(needle), text, { timeout: 10000 })
}

export default async function (page, { base }) {
  await page.goto(base + '/setup-help')
  await waitForText(page, 'Forge setup help')

  // The tab title uses the new naming ("Getting set up · Forge"), and the page
  // itself says what it is before the rendered document starts.
  assert.equal(await page.title(), 'Getting set up · Forge')
  // CSS text-transform:uppercase renders the eyebrow visually as "HELP";
  // the DOM text content (what the markup actually says) stays "Help".
  assert.equal((await page.locator('#help-eyebrow').textContent())?.trim(), 'Help')
  assert.equal((await page.locator('h1').innerText()).trim(), 'Forge setup help')

  // Every heading in the fixture got a slugified id — the two other pages
  // deep-link to (#host-installation, #github-repository-protection) and the
  // rest — and the sidebar links to all seven, in document order.
  for (const heading of FIXTURE_HEADINGS) {
    const tag = `h${heading.level}`
    const el = page.locator(`${tag}#${heading.id}`)
    assert.equal(await el.count(), 1, `missing ${tag}#${heading.id}`)
    assert.equal((await el.innerText()).trim(), heading.text)
  }
  const sidebarLinks = page.locator('nav[aria-label="Help topics"] a')
  assert.equal(await sidebarLinks.count(), FIXTURE_HEADINGS.length)
  for (let index = 0; index < FIXTURE_HEADINGS.length; index += 1) {
    const link = sidebarLinks.nth(index)
    assert.equal((await link.innerText()).trim(), FIXTURE_HEADINGS[index].text)
    assert.equal(await link.getAttribute('href'), `/setup-help#${FIXTURE_HEADINGS[index].id}`)
  }
  assert.equal((await page.locator('nav[aria-label="Help topics"] p').textContent())?.trim(), 'On this page')

  // The result is server-rendered sanitized HTML (v-html), never a raw
  // <script> — same defence the issue body renderer gets.
  assert.equal(await page.locator('.forge-help script').count(), 0)

  // A real same-origin asset is requested and decoded, with reserved space.
  const hero = page.locator('.forge-help img').first()
  await hero.scrollIntoViewIfNeeded()
  await hero.evaluate(img => img.decode())
  assert.equal(await hero.getAttribute('src'), '/_ui/help-assets/setup-hero.svg')
  assert.equal(await hero.getAttribute('alt'), 'Four role cubes')
  assert.equal(await hero.getAttribute('loading'), 'lazy')
  assert.ok(await hero.evaluate(img => img.complete && img.naturalWidth === 600))
  assert.equal(await page.locator('.forge-help img').count(), 1, 'external images must be stripped')
  const asset = await page.request.get(base + '/_ui/help-assets/setup-hero.svg')
  assert.equal(asset.status(), 200)
  assert.match(asset.headers()['content-type'], /^image\/svg\+xml/)
  assert.equal(asset.headers()['x-content-type-options'], 'nosniff')
  for (const name of ['unknown.svg', 'constructor', '__proto__', 'package.json', '%2e%2e%2fpackage.json']) {
    assert.equal((await page.request.get(base + '/_ui/help-assets/' + name)).status(), 404)
  }

  // The header's help icon reaches this same page from anywhere in the app.
  await page.goto(base + '/')
  await page.getByRole('link', { name: 'Help' }).click()
  await page.waitForURL(base + '/setup-help')
  await waitForText(page, 'Forge setup help')

  // A deep link used elsewhere in the UI (operator_status.overview's
  // "Check service setup" action, the repository protection panel's "Read
  // the setup guide") lands on that heading, scrolled clear of the sticky
  // header rather than hidden underneath it.
  await page.goto(base + '/setup-help#host-installation')
  await waitForText(page, 'Forge setup help')
  await page.waitForFunction(() => {
    const el = document.getElementById('host-installation')
    return !!el && el.getBoundingClientRect().top < 200
  }, null, { timeout: 10000 })
  const rect = await page.locator('#host-installation').evaluate((el) => el.getBoundingClientRect().top)
  assert.ok(rect >= 0 && rect < 200, `#host-installation should be scrolled near the top, got ${rect}`)

  // Guide navigation is client-side; the diagrams are actual SVGs and tables
  // remain structured HTML. Reload and Back preserve the selected guide.
  await page.getByRole('link', { name: 'How an issue moves', exact: true }).click()
  await page.waitForURL(base + '/setup-help?topic=workflow')
  await page.locator('.help-diagram svg').nth(2).waitFor({ timeout: 30000 })
  assert.equal(await page.locator('.help-diagram svg').count(), 3)
  assert.equal(await page.locator('.forge-help table').count(), 2)
  await page.getByRole('button', { name: 'Fit diagram', exact: true }).first().click()
  assert.equal(await page.getByRole('button', { name: 'Actual size', exact: true }).getAttribute('aria-pressed'), 'true')
  await page.getByRole('button', { name: 'Actual size', exact: true }).click()
  assert.equal(await page.getByRole('link', { name: 'How an issue moves', exact: true }).getAttribute('aria-current'), 'page')
  await page.reload()
  await page.locator('.help-diagram svg').nth(2).waitFor({ timeout: 30000 })
  for (const width of [320, 390, 1440]) {
    await page.setViewportSize({ width, height: 900 })
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `workflow overflow at ${width}`)
    await page.screenshot({ path: `/tmp/forge-help-${width}.png`, fullPage: true })
  }
  await page.getByRole('link', { name: 'Talking to a worker', exact: true }).click()
  await page.waitForURL(base + '/setup-help?topic=discussions')
  await page.locator('.forge-help h1').waitFor()
  await page.locator('.help-diagram svg').nth(1).waitFor({ timeout: 30000 })
  assert.equal(await page.locator('.help-diagram svg').count(), 2)
  await page.goBack()
  await page.locator('.help-diagram svg').nth(2).waitFor({ timeout: 30000 })

  // Unknown topics never resolve arbitrary files and keep recovery navigation.
  const invalid = await page.request.get(base + '/_ui/help?topic=../../package.json')
  assert.equal(invalid.status(), 404)
  await page.goto(base + '/setup-help?topic=unknown')
  await waitForText(page, 'Help topic not found')
  await page.getByRole('link', { name: 'Getting set up', exact: true }).click()
  await waitForText(page, 'Forge setup help')

  // A guide fetch failure offers retry without losing the guide menu.
  await page.route('**/_ui/help?topic=runtime', route => route.fulfill({ status: 503, contentType: 'application/json', body: '{"statusMessage":"Unavailable"}' }))
  await page.getByRole('link', { name: 'Workers, accounts and merging', exact: true }).click()
  await waitForText(page, 'Guide unavailable')
  await page.unroute('**/_ui/help?topic=runtime')
  await page.getByRole('button', { name: 'Try again' }).click()
  await waitForText(page, 'Workers, accounts and merging')

  // Every guide's bundled assets decode at phone width in both colour schemes.
  // The app's existing dark shell is unchanged; illustrations support either scheme.
  for (const colorScheme of ['light', 'dark']) {
    await page.emulateMedia({ colorScheme, reducedMotion: 'reduce' })
    for (const topic of ['setup', 'workflow', 'discussions', 'runtime']) {
      await page.setViewportSize({ width: 320, height: 900 })
      await page.goto(base + '/setup-help?topic=' + topic)
      await page.locator('.forge-help img').first().waitFor()
      for (const img of await page.locator('.forge-help img').all()) {
        await img.scrollIntoViewIfNeeded()
        await img.evaluate(el => el.decode())
        assert.ok(await img.evaluate(el => el.naturalWidth > 0 && el.clientWidth <= innerWidth))
      }
      const expectedDiagrams = { setup: 0, workflow: 3, discussions: 2, runtime: 3 }[topic]
      if (expectedDiagrams) await page.locator('.help-diagram svg').nth(expectedDiagrams - 1).waitFor({ timeout: 30000 })
      assert.equal(await page.getByText('Diagram unavailable.', { exact: false }).count(), 0, 'all diagrams must render')
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${topic} ${colorScheme} overflow`)
      await page.screenshot({ path: `/tmp/forge-help-${topic}-${colorScheme}.png`, fullPage: true })
    }
  }

  // On a phone-width viewport nothing scrolls sideways.
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(base + '/setup-help')
  await waitForText(page, 'Forge setup help')
  const overflow = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth,
  }))
  assert.ok(overflow.scrollWidth <= overflow.innerWidth, `/setup-help scrolls sideways: ${JSON.stringify(overflow)}`)
}
