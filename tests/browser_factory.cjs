const renderPage=require('./browser_page.cjs');
const {chromium}=require(process.env.FORGE_PLAYWRIGHT_MODULE||'playwright-core');
const fs=require('fs'),path=require('path'),assert=require('assert');
(async()=>{const browser=await chromium.launch({chromiumSandbox:true,headless:true,executablePath:process.env.FORGE_BROWSER_PATH,args:[]});try{
const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],actions=[];
page.on('pageerror',e=>errors.push(e.message));
const issue={key:'1:1',repo:'example/project',number:1,title:'Repair issue-to-PR publication',state:'ready',active:'11111111-1111-4111-8111-111111111111',failure_count:0,failures:[]};
const failed={...issue,key:'1:2',number:2,title:'Stop repeated failures',state:'failed',active:null,failure_count:3,reason:'Three attempts failed',failures:[{cause:'<img src=x onerror="window.injected=1">',findings:{summary:'Repeated test failures',alternative:null}}]};
let offline=false;
const fixture={ok:true,observation:{at:Date.now()/1000,errors:[]},controls_token:'synthetic',active:true,runtime:{enabled:true,slots:{build:4,review:2,audit:1}},slots:{'build-1':issue.active},issues:[issue,failed],jobs:[{id:issue.active,role:'build',slot:'build-1',phase:'running',cleanup:false,observed_at:Date.now()/1000,started_at:Date.now()/1000-180}]};
await page.route('http://forge.test/**',route=>{const url=new URL(route.request().url());if(url.pathname==='/factory')return route.fulfill({contentType:'text/html',body:renderPage('factory.html',url.pathname)});if(url.pathname==='/api/factory')return offline?route.fulfill({status:503}):route.fulfill({json:fixture});if(url.pathname==='/api/factory/output')return route.fulfill({json:{ok:true,logs:'<script>window.injected=1</script>\nCaptured output'}});if(url.pathname==='/api/factory/action'){assert.equal(route.request().headers()['x-forge-action'],'synthetic');actions.push(route.request().postDataJSON());return route.fulfill({json:{ok:true,request:'one'}})}return route.fulfill({status:404})});
await page.goto('http://forge.test/factory');await page.getByText('Repair issue-to-PR publication').waitFor();
await page.getByRole('link',{name:'Forge home',exact:true}).waitFor();
const navigation=page.getByRole('navigation',{name:'Main navigation'});
assert.equal(await navigation.getByRole('link',{name:'Settings',exact:true}).getAttribute('href'),'/settings');
assert.equal(await navigation.getByRole('link').count(),2);
assert.equal(await page.getByRole('button',{name:'Stop',exact:true}).count(),1);
assert.equal(await page.getByRole('button',{name:'Release',exact:true}).count(),0);
await page.getByRole('button',{name:'Stop',exact:true}).click();await page.getByRole('dialog').waitFor();assert.equal(actions.length,0);
await page.getByRole('button',{name:'Close',exact:true}).click();assert.equal(actions.length,0);
await page.getByRole('button',{name:'Output',exact:true}).click();await page.getByText('Captured output',{exact:false}).waitFor();assert.equal(await page.evaluate(()=>window.injected),undefined);await page.getByRole('button',{name:'Close',exact:true}).click();
assert.equal(await page.getByRole('button',{name:'Retry',exact:true}).count(),0);
assert.equal(await page.locator('a[href="/issues?issue=1%3A2"]').count(),1);
fixture.operator={title:'Building issue #1',detail:issue.title,action_url:'https://github.com/example/project/issues/1',action_label:'View issue'};
await page.reload();await page.getByText('Building issue #1',{exact:true}).waitFor();assert.equal(await page.locator('h1').textContent(),'Factory');
assert.equal(await page.locator('#next-action a').getAttribute('href'),'https://github.com/example/project/issues/1');
// Polling retains cards and focused controls; transitions move the same node.
const original=await page.locator('[data-key="1:1"]').elementHandle();
await page.locator('[data-key="1:1"] [data-issue-view]').focus();
await page.evaluate(()=>poller.refresh());
assert(await original.evaluate(el=>el.isConnected));
assert.equal(await page.evaluate(()=>document.activeElement.dataset.issueView),'1:1');
assert.equal(await page.locator('.issue-card.is-working').count(),1);
assert.equal(await page.locator('.metric[data-role="build"] .metric-value').textContent(),'1');
assert.match(await page.locator('.metric[data-role="build"] .elapsed').textContent(),/3 min/);
// Work duration advances locally between polls, using the container start time.
await page.evaluate(()=>{window.realNow=Date.now;Date.now=()=>window.realNow()+61000;tick()});
assert.match(await page.locator('.metric[data-role="build"] .elapsed').textContent(),/4 min/);
await page.evaluate(()=>{Date.now=window.realNow;tick()});
assert.notEqual(await page.locator('.is-working .work-wave.top').evaluate(el=>getComputedStyle(el,'::after').animationPlayState),'paused');
fixture.issues[0].state='built';fixture.jobs[0].role='review';fixture.jobs[0].slot='review-1';
await page.evaluate(()=>poller.refresh());
assert(await original.evaluate(el=>el.parentElement.id==='cards-review'));
assert.equal(await page.locator('#cards-build .issue-card').count(),0);
assert.equal(await page.locator('#cards-review .issue-card').count(),1);
assert.equal(await page.evaluate(()=>document.activeElement.dataset.issueView),'1:1');
assert.equal(await page.locator('.metric[data-role="review"] .metric-value').textContent(),'1');
// Internal recovery never inflates productive work statistics.
fixture.jobs.push({id:'recovery',role:'recovery',phase:'running',observed_at:Date.now()/1000,started_at:Date.now()/1000-90});
await page.evaluate(()=>poller.refresh());
assert.equal(await page.locator('.metrics .working-spinner').count(),1);
await page.emulateMedia({reducedMotion:'reduce'});
assert.equal(await page.locator('.is-working .work-wave.top').evaluate(el=>getComputedStyle(el,'::after').animationName),'none');
await page.emulateMedia({reducedMotion:'no-preference'});
offline=true;await page.evaluate(()=>poller.refresh());
assert(await original.evaluate(el=>el.isConnected));
assert.equal(await page.locator('.issue-card.is-working').count(),0);
assert.match(await page.locator('#freshness').textContent(),/last known/);
assert.equal(await page.locator('.metric[data-role="review"] .metric-value').textContent(),'1');
const frozenTime=await page.locator('.metric[data-role="review"] .elapsed').textContent();
await page.evaluate(()=>{Date.now=()=>window.realNow()+61000;tick()});
assert.equal(await page.locator('.metric[data-role="review"] .elapsed').textContent(),frozenTime);
await page.evaluate(()=>{Date.now=window.realNow;tick()});
offline=false;await page.evaluate(()=>poller.refresh());assert.equal(await page.locator('.issue-card.is-working').count(),1);
fixture.observation.at=Date.now()/1000-600;await page.evaluate(()=>poller.refresh());assert.equal(await page.locator('.issue-card.is-working').count(),0);
fixture.observation.at=Date.now()/1000;fixture.jobs[0].observed_at=Date.now()/1000;
const auth={...failed,key:'1:3',number:3,state:'ready',title:'Queued authentication check',failure_count:0,reason:'blocked_auth',history:[{kind:'availability_wait',provider:'claude'}]};fixture.issues.push(auth);
await page.evaluate(()=>poller.refresh());await page.getByText('Claude needs sign-in.',{exact:false}).waitFor();
assert.equal(await page.locator('[data-key="1:3"].is-working').count(),0);
assert.equal(await page.locator('.metric[data-key="queued"] .metric-value').textContent(),'1');
await page.screenshot({path:'/tmp/forge-factory-live-desktop.png',fullPage:true});
for(const width of [320,390,760]){await page.setViewportSize({width,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));}
await page.setViewportSize({width:390,height:844});await page.waitForTimeout(450);
await page.screenshot({path:'/tmp/forge-factory-live-mobile.png',fullPage:true});
await page.setViewportSize({width:1440,height:1000});

fixture.issues=[];fixture.jobs=[];fixture.slots={};
for(const [title,detail,url,label] of [
 ['Waiting for the Forge service','The supervisor has not reported recently.','/setup-help#host-installation','Check service setup'],
 ['Check repository setup','example/project needs a review-settings check.','/settings/repository?repo_id=1','Check repository'],
 ['Waiting for a coding account','Forge will retry when it can.','/settings/harnesses','Review coding accounts'],
 ['An issue needs your attention','An authorized human must approve.','https://github.com/example/project/pull/12','Review pull request'],
 ['Investigating issue #1','Forge is checking the failure before deciding the next attempt.','https://github.com/example/project/issues/1','View issue'],
 ['Waiting for a ready issue','Add the ready label to an issue in an enabled repository.','https://github.com/example/project/issues','Open GitHub issues']]){
 fixture.operator={title,detail,action_url:url,action_label:label};await page.reload();await page.getByText(title,{exact:true}).waitFor();
 assert.equal(await page.locator('#next-action a').getAttribute('href'),url);
}
fixture.issues=[{...issue,state:'merged',active:null,candidate:{pr:12}}];
await page.reload();await page.getByText('Completed issues',{exact:true}).click();
await page.locator('#completed').getByRole('link',{name:'PR #12 ↗',exact:true}).waitFor();
await page.screenshot({path:'/tmp/forge-factory-desktop.png',fullPage:true});for(const width of [320,390]){await page.setViewportSize({width,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));}await page.screenshot({path:'/tmp/forge-factory-mobile.png',fullPage:true});assert.deepEqual(errors,[]);console.log('Factory browser checks passed: compact actions, confirmation, issue detail links, escaped output, responsive layout.');
}finally{await browser.close()}})().catch(e=>{console.error(e);process.exit(1)});
