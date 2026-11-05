import assert from 'node:assert/strict'

export default async function (page, { base }) {
  await page.goto(base + '/settings/connections')
  await page.getByRole('heading', { name: 'Connections', exact: true }).waitFor()
  const navigation = page.getByRole('navigation', { name: 'Settings navigation' })
  assert.deepEqual(await navigation.getByRole('link').allTextContents(), ['General', 'Repositories', 'Connections'])
  for (const [title, route] of [['Coding accounts', 'harnesses'], ['GitHub', 'github'], ['External agents', 'agents']]) {
    await page.getByRole('link', { name: new RegExp('^' + title) }).click()
    await page.waitForURL(base + '/settings/' + route)
    assert.equal(await navigation.getByRole('link', { name: 'Connections', exact: true }).getAttribute('aria-current'), 'page')
    await page.getByRole('link', { name: '← Connections', exact: true }).click()
    await page.waitForURL(base + '/settings/connections')
  }
  for (const width of [320, 390]) {
    await page.setViewportSize({ width, height: 844 })
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
  }
  await page.screenshot({ path: '/private/tmp/forge-connections-mobile.png', fullPage: true })
}
