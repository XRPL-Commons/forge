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

  // The tab title uses the new naming ("Setup help · Forge"), and the page
  // itself says what it is before the rendered document starts.
  assert.equal(await page.title(), 'Setup help · Forge')
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
    assert.equal(await link.getAttribute('href'), `#${FIXTURE_HEADINGS[index].id}`)
  }
  assert.equal((await page.locator('nav[aria-label="Help topics"] p').textContent())?.trim(), 'On this page')

  // The result is server-rendered sanitized HTML (v-html), never a raw
  // <script> — same defence the issue body renderer gets.
  assert.equal(await page.locator('.forge-help script').count(), 0)

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

  // On a phone-width viewport nothing scrolls sideways.
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(base + '/setup-help')
  await waitForText(page, 'Forge setup help')
  const overflow = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth,
  }))
  assert.ok(overflow.scrollWidth <= overflow.innerWidth, `/setup-help scrolls sideways: ${JSON.stringify(overflow)}`)
}
