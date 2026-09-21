const renderPage=require('./browser_page.cjs');
const {chromium}=require(process.env.FORGE_PLAYWRIGHT_MODULE||'playwright-core');
const assert=require('assert');
(async()=>{
 const browser=await chromium.launch({chromiumSandbox:true,headless:true,executablePath:process.env.FORGE_BROWSER_PATH});
 try{
  const page=await browser.newPage({viewport:{width:390,height:844}}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  const now=Date.now()/1000;
  let status={ok:true,active:true,runtime:{enabled:true,cycle_seconds:30},observation:{at:now},jobs:[{role:'build',phase:'running',last_observed:now,cleanup:false}]};
  await page.route('http://forge.test/**',route=>{
   const path=new URL(route.request().url()).pathname;
   if(path==='/api/factory')return route.fulfill({json:status});
   return route.fulfill({contentType:'text/html',body:renderPage('help',path)});
  });
  for(const route of ['/','/factory','/issues']){
   await page.goto('http://forge.test'+route);
   const tabs=page.getByRole('navigation',{name:'Work navigation'});
   assert.deepEqual(await tabs.getByRole('link').allTextContents(),['Activity','Issues']);
   assert.equal(await tabs.locator('[aria-current="page"]').textContent(),route==='/issues'?'Issues':'Activity');
  }
  const activity=page.locator('#forge-activity');
  async function check(label){await page.goto('http://forge.test/setup-help',{waitUntil:'networkidle'});if(label)await page.getByRole('link',{name:label,exact:true}).waitFor();else assert(await activity.isHidden());assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));}
  await check('1 builder working');
  assert.equal(await activity.locator('a').getAttribute('href'),'/factory#stage-build');
  await page.emulateMedia({reducedMotion:'reduce'});
  assert.equal(await activity.locator('.worker-spinner').evaluate(el=>getComputedStyle(el).animationName),'none');
  status.jobs=[{role:'recovery',phase:'running',last_observed:now}];await check(null);
  status.jobs=[];await check(null);
  status.runtime.enabled=false;await check(null);
  status.issues=[{state:'ready',repo:'example/project'},{state:'built',repo:'example/project'},{state:'hold',repo:'example/project'},{state:'ready',active:'job'},{state:'ready',repo:'example/disabled'}];
  status.repositories=[{name:'example/project',enabled:true},{name:'example/disabled',enabled:false}];
  await check('2 queued issues');assert.equal(await activity.locator('.worker-spinner').count(),0);
  status.jobs=['build','build','audit'].map(role=>({role,phase:'running',last_observed:now}));
  await check('2 builders working');await page.getByRole('link',{name:'1 auditor working',exact:true}).waitFor();
  await page.locator('.forge-topbar').screenshot({path:'/tmp/forge-compact-activity-mobile.png'});
  await page.setViewportSize({width:320,height:740});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.setViewportSize({width:1280,height:900});await page.locator('.forge-topbar').screenshot({path:'/tmp/forge-compact-activity-desktop.png'});
  status.observation.at=now-600;await check(null);
  status.observation.at=now;status.issues=[];status.jobs=[{role:'build',phase:'running',last_observed:now-600}];await check(null);
  status={ok:false};await check(null);
  status={ok:true,active:false};await check(null);
  await page.goto('http://forge.test/settings/harnesses');
  const nav=page.getByRole('navigation',{name:'Main navigation'});
  assert.equal(await nav.getByRole('link',{name:'Settings',exact:true}).getAttribute('href'),'/settings');
  assert.equal(await nav.getByRole('link',{name:'Settings',exact:true}).getAttribute('aria-current'),'page');
  assert.equal(await page.getByRole('link',{name:'Forge home',exact:true}).getAttribute('href'),'/');
  assert.equal(await nav.getByRole('link',{name:'Help',exact:true}).getAttribute('href'),'/setup-help');
  assert.equal(await page.getByRole('navigation',{name:'Settings navigation'}).locator('[aria-current=page]').textContent(),'Coding accounts');
  await page.setViewportSize({width:320,height:740});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  assert.deepEqual(errors,[]);
  console.log('Navigation checks passed: current activity, stale/failed observations, paused dispatch, reduced motion, Settings tabs and narrow layout.');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1)});
