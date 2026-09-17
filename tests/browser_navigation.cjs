const renderPage=require('./browser_page.cjs');
const {chromium}=require(process.env.FORGE_PLAYWRIGHT_MODULE||'playwright-core');
const assert=require('assert');
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.FORGE_BROWSER_PATH,args:['--no-sandbox']});
 try{
  const page=await browser.newPage({viewport:{width:390,height:844}}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  const now=Date.now()/1000;
  let status={ok:true,active:true,runtime:{enabled:true,cycle_seconds:30},observation:{at:now},jobs:[{phase:'running',last_observed:now,cleanup:false}]};
  await page.route('http://forge.test/**',route=>{
   const path=new URL(route.request().url()).pathname;
   if(path==='/api/factory')return route.fulfill({json:status});
   return route.fulfill({contentType:'text/html',body:renderPage('help',path)});
  });
  const activity=page.locator('#forge-activity');
  async function check(label){await page.goto('http://forge.test/setup-help');await page.getByText(label,{exact:true}).waitFor();assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));}
  await check('1 working');assert.equal(await activity.getAttribute('data-state'),'working');
  await page.emulateMedia({reducedMotion:'reduce'});
  assert.equal(await activity.evaluate(el=>getComputedStyle(el,'::before').animationName),'none');
  status.jobs=[];await check('Idle');
  status.runtime.enabled=false;await check('Paused');
  status.observation.at=now-600;await check('Status delayed');assert.equal(await activity.getAttribute('data-state'),'unknown');
  status.observation.at=now;status.jobs=[{phase:'running',last_observed:now-600}];await check('Status delayed');
  status={ok:false};await check('Status unavailable');
  status={ok:true,active:false};await page.reload();assert(await activity.isHidden());
  await page.goto('http://forge.test/settings/harnesses');
  const nav=page.getByRole('navigation',{name:'Main navigation'});
  assert.equal(await nav.getByRole('link',{name:'← Factory',exact:true}).getAttribute('href'),'/');
  assert.equal(await nav.getByRole('link',{name:'Help',exact:true}).getAttribute('href'),'/setup-help');
  assert.equal(await page.getByRole('navigation',{name:'Settings navigation'}).locator('[aria-current=page]').textContent(),'Coding accounts');
  await page.setViewportSize({width:320,height:740});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  assert.deepEqual(errors,[]);
  console.log('Navigation checks passed: current activity, stale/failed observations, paused dispatch, reduced motion, Settings tabs and narrow layout.');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1)});
