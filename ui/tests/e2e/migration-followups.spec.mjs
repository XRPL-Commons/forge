import assert from 'node:assert/strict'

const waitText = (page, text) => page.waitForFunction(text => document.body.innerText.includes(text), text)
const noOverflow = page => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)

export default async function (page, { base, scenario }) {
  await page.goto(base + '/')
  await page.locator('#factory-line').waitFor()

  // A hash can target the collapsed investigations line on the current page.
  await page.locator('#runners details').waitFor()
  assert.equal(await page.locator('#runners details').evaluate(details => details.open), false)
  await page.evaluate(() => { location.hash = '#stage-recovery' })
  await page.waitForFunction(() => document.querySelector('#runners details').open)
  await page.waitForFunction(() => document.querySelector('#stage-recovery').getBoundingClientRect().top < innerHeight)
  // It also works when data arrives after direct navigation.
  await page.goto(base + '/#stage-recovery')
  await page.waitForFunction(() => document.querySelector('#runners details')?.open)

  await scenario('paused')
  await waitText(page, 'Factory reset is in progress')
  await scenario('down')
  await waitText(page, 'Reconnecting · last known state')
  assert.equal(await page.locator('#factory-line').count(), 0, 'a failed poll cannot revive the fenced board')
  assert.ok(!(await page.locator('body').innerText()).includes('Factory reset is in progress'), 'an old fence is not a fresh observation')
  for (const width of [320, 390, 1440]) {
    await page.setViewportSize({width,height:900})
    assert.ok(await noOverflow(page), `fence failure overflow at ${width}`)
  }

  // The API is allowed to return an applied/rejected action with no message.
  // A previous decision must not hide the receipt for a new request.
  await scenario('decision-queue')
  let actions = [{id:'old',key:'1:609',status:'applied',message:null}]
  await page.route('**/api/issues?issue=*', async route => {
    const response = await route.fetch({ headers: { ...route.request().headers(), 'if-none-match': '' } })
    const body = await response.json()
    body.actions = actions
    await route.fulfill({response, json:body})
  })
  await page.route('**/api/issues/action', route => route.fulfill({json:{ok:true,request:'new'}}))
  await page.goto(base + '/issues/1%3A609')
  await waitText(page, 'Decision applied.')
  await page.getByRole('button', {name:'Queue for Forge',exact:true}).click()
  await page.getByRole('dialog').getByRole('button', {name:'Queue for Forge',exact:true}).click()
  await waitText(page, 'Decision submitted. Waiting for confirmation.')
  actions = [...actions, {id:'new',key:'1:609',status:'pending',message:null}]
  await waitText(page, 'Applying your decision…')
  actions = [{id:'new',key:'1:609',status:'applied',message:null}]
  await waitText(page, 'Decision applied.')
  actions = []
  await page.waitForFunction(() => !document.querySelector('#detail-message')?.textContent.trim())
  assert.ok(!(await page.locator('body').innerText()).includes('Waiting for confirmation'), 'the acknowledged receipt must not return')
  actions = [{id:'rejected',key:'1:609',status:'rejected',message:null}]
  await waitText(page, 'Decision rejected. Refresh the issue before trying again.')
  for (const width of [320,390,1440]) {
    await page.setViewportSize({width,height:900})
    assert.ok(await noOverflow(page), `decision receipt overflow at ${width}`)
  }
  await page.unroute('**/api/issues?issue=*')
  await page.unroute('**/api/issues/action')

  await scenario('default')
  await page.goto(base + '/onboarding')
  await page.locator('main h1').waitFor()
  assert.equal(await page.locator('main').count(), 1, 'the layout owns the only main landmark')

  // Missing App access keeps the refresh control visible, without permitting it.
  await page.route('**/api/repositories?repo_id=2', async route => {
    const response = await route.fetch({ headers: { ...route.request().headers(), 'if-none-match': '' } })
    const body = await response.json()
    for (const repo of body.repositories ?? []) { repo.protection_status = 'access_needed'; repo.access_ready = false }
    await route.fulfill({response,json:body})
  })
  await page.goto(base + '/settings/repository?repo_id=2')
  await page.waitForFunction(() => [...document.querySelectorAll('button')].some(button => button.textContent.trim() === 'Refresh repository' && button.disabled))
  await page.unroute('**/api/repositories?repo_id=2')
}
