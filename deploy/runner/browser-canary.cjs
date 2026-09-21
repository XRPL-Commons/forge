// No network or repository imports. Prove browser execution, not binary presence.
const {chromium} = require('/usr/local/lib/node_modules/playwright-core');
(async () => {
  const browser = await chromium.launch({headless: true, executablePath: '/usr/bin/chromium'});
  try {
    const page = await browser.newPage();
    await page.goto('data:text/html,<title>Forge browser canary</title>');
    if (await page.title() !== 'Forge browser canary') throw Error('Browser execution failed');
    console.log('forge-browser-ok');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
