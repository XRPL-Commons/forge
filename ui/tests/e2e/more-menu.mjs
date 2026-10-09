// Ending work (Stop attempt, Reset runner, Close issue) lives behind the
// action bar's "More" menu; each item opens its own confirmation.
export async function chooseMore(page, root, item) {
  await root.getByRole('button', { name: 'More actions' }).click()
  const entry = page.getByRole('menuitem', { name: item })
  await entry.waitFor({ timeout: 10000 })
  await entry.click()
}

export async function moreItems(page, root) {
  const button = root.getByRole('button', { name: 'More actions' })
  if (!await button.count()) return []
  await button.click()
  await page.getByRole('menu').waitFor()
  const items = (await page.getByRole('menuitem').allInnerTexts()).map(text => text.trim())
  await page.keyboard.press('Escape')
  await page.getByRole('menu').waitFor({ state: 'detached' })
  return items
}
