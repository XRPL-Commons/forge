const renderPage=require('./browser_page.cjs');
const {chromium}=require(process.env.FORGE_PLAYWRIGHT_MODULE||'playwright-core');
const fs=require('fs'),path=require('path'),assert=require('assert');
(async()=>{const browser=await chromium.launch({chromiumSandbox:true,headless:true,executablePath:process.env.FORGE_BROWSER_PATH,args:[]});try{
const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],actions=[];
page.on('pageerror',e=>errors.push(e.message));
const issue={key:'1:1',repo:'example/project',number:1,title:'Repair issue-to-PR publication',state:'ready',active:'11111111-1111-4111-8111-111111111111',failure_count:0,failures:[]};
const failed={...issue,key:'1:2',number:2,title:'Stop repeated failures',state:'failed',active:null,failure_count:3,reason:'Three attempts failed',failures:[{cause:'<img src=x onerror="window.injected=1">',findings:{summary:'Repeated test failures',alternative:null}}]};
const fixture={ok:true,observation:{at:Date.now()/1000,errors:[]},controls_token:'synthetic',active:true,runtime:{enabled:true,slots:{build:4,review:2,audit:1}},slots:{'build-1':issue.active},issues:[issue,failed],jobs:[{id:issue.active,role:'build',slot:'build-1',phase:'running',cleanup:false}]};
await page.route('http://forge.test/**',route=>{const url=new URL(route.request().url());if(url.pathname==='/factory')return route.fulfill({contentType:'text/html',body:renderPage('factory.html',url.pathname)});if(url.pathname==='/api/factory')return route.fulfill({json:fixture});if(url.pathname==='/api/factory/output')return route.fulfill({json:{ok:true,logs:'<script>window.injected=1</script>\nCaptured output'}});if(url.pathname==='/api/factory/action'){assert.equal(route.request().headers()['x-forge-action'],'synthetic');actions.push(route.request().postDataJSON());return route.fulfill({json:{ok:true,request:'one'}})}return route.fulfill({status:404})});
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
await page.reload();await page.getByRole('heading',{name:'Building issue #1',exact:true}).waitFor();
assert.equal(await page.locator('#next-action a').getAttribute('href'),'https://github.com/example/project/issues/1');
fixture.issues=[];fixture.jobs=[];fixture.slots={};
for(const [title,detail,url,label] of [
 ['Waiting for the Forge service','The supervisor has not reported recently.','/setup-help#host-installation','Check service setup'],
 ['Check repository setup','example/project needs a review-settings check.','/settings/repository?repo_id=1','Check repository'],
 ['Waiting for a coding account','Forge will retry when it can.','/settings/harnesses','Review coding accounts'],
 ['An issue needs your attention','An authorized human must approve.','https://github.com/example/project/pull/12','Review pull request'],
 ['Investigating issue #1','Forge is checking the failure before deciding the next attempt.','https://github.com/example/project/issues/1','View issue'],
 ['Waiting for a ready issue','Add the ready label to an issue in an enabled repository.','https://github.com/example/project/issues','Open GitHub issues']]){
 fixture.operator={title,detail,action_url:url,action_label:label};await page.reload();await page.getByRole('heading',{name:title,exact:true}).waitFor();
 assert.equal(await page.locator('#next-action a').getAttribute('href'),url);
}
fixture.issues=[{...issue,state:'merged',active:null,candidate:{pr:12}}];
await page.reload();await page.getByText('Completed issues',{exact:true}).click();
await page.locator('#completed').getByRole('link',{name:'PR #12 ↗',exact:true}).waitFor();
await page.screenshot({path:'/tmp/forge-factory-desktop.png',fullPage:true});for(const width of [320,390]){await page.setViewportSize({width,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));}await page.screenshot({path:'/tmp/forge-factory-mobile.png',fullPage:true});assert.deepEqual(errors,[]);console.log('Factory browser checks passed: compact actions, confirmation, retry reason, escaped output, responsive layout.');
}finally{await browser.close()}})().catch(e=>{console.error(e);process.exit(1)});
