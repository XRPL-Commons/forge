const renderPage=require('./browser_page.cjs');
const {chromium}=require(process.env.FORGE_PLAYWRIGHT_MODULE||'playwright-core');
const fs=require('fs'),path=require('path'),assert=require('assert');
(async()=>{const browser=await chromium.launch({chromiumSandbox:true,headless:true,executablePath:process.env.FORGE_BROWSER_PATH});try{
const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],writes=[];
page.setDefaultTimeout(10000);
page.on('pageerror',e=>errors.push(e.message));
const base={repo:'XRPL-Commons/academy',repo_id:1,pull_requests:[],dependencies:[],fresh:true,setup_needed:false,setup_url:'/settings/repository?repo_id=1',revision:'current',history:[],body:'A clear description of the expected work.',active:null};
const rows=[
 {...base,key:'1:613',number:613,title:'Redact credential values at the logging boundary',status:'attention',explanation:'Repository setup now passes. Recheck to resume.',actions:['recheck'],setup_needed:true},
 {...base,key:'1:585',number:585,repo:'XRPL-Commons/hackathon',title:'Finish the migration off legacy fields',status:'attention',explanation:'Paused by hold on GitHub.',actions:['resume'],dependencies:[584]},
 {...base,key:'1:610',number:610,title:'Make course navigation easier',status:'working',explanation:'Building',actions:[],active:'attempt'},
 {...base,key:'1:609',number:609,title:'Improve the course completion email',status:'backlog',explanation:'Not queued for Forge.',actions:['queue'],body:'<img src=x onerror="window.injected=1">\nWrite a useful completion email.'},
 {...base,key:'1:608',number:608,title:'Keep learners signed in',status:'queued',explanation:'Queued for Forge.',actions:['backlog','pause']},
 {...base,key:'1:600',number:600,title:'Repair old links',status:'completed',explanation:'Closed on GitHub.',actions:[]}
];
let pending=[],fail=false;
await page.route('http://forge.test/**',async route=>{const req=route.request(),url=new URL(req.url());
 if(url.pathname==='/issues')return route.fulfill({contentType:'text/html',body:renderPage('issues.html',url.pathname)});
 if(url.pathname==='/assets/issues.js')return route.fulfill({contentType:'application/javascript',body:fs.readFileSync(path.join(__dirname,'../legacy/issues.js'),'utf8')});
 if(req.method()==='POST'){writes.push({path:url.pathname,body:req.postDataJSON()});assert.equal(req.headers()['x-forge-action'],'synthetic');if(fail)return route.fulfill({status:409,json:{ok:false,reason:'This issue changed. Refresh it before applying the decision.'}});pending=[{id:'decision',key:req.postDataJSON().key,status:'pending'}];return route.fulfill({json:{ok:true,request:'decision'}})}
 if(url.pathname==='/api/issues'){
  const envelope={ok:true,controls_token:'synthetic',repositories:[{id:1,name:'XRPL-Commons/academy',complete:true,fresh:true},{id:2,name:'XRPL-Commons/hackathon',complete:true,fresh:true}],actions:pending};
  if(url.searchParams.has('issue'))return route.fulfill({json:{...envelope,issue:rows.find(r=>r.key===url.searchParams.get('issue'))}});
  const state=url.searchParams.get('state'),query=(url.searchParams.get('q')||'').toLowerCase();const filtered=rows.filter(r=>(state==='open'?r.status!=='completed':r.status===state)&&r.title.toLowerCase().includes(query));
  return route.fulfill({json:{...envelope,issues:filtered,total:filtered.length,page:1,pages:1,complete:true}});
 }return route.fulfill({status:404})});
await page.goto('http://forge.test/issues');await page.getByText('Redact credential values at the logging boundary',{exact:true}).waitFor();
assert.equal(writes.length,0);assert.equal(await page.locator('#rows tr').count(),5);
await page.getByLabel('Find an issue').fill('completion');await page.waitForFunction(()=>document.querySelectorAll('#rows tr').length===1);
assert.equal(await page.locator('#search').inputValue(),'completion');assert.equal(await page.locator('#search').evaluate(e=>e===document.activeElement),true);
await page.locator('#rows').getByRole('button',{name:'Queue',exact:true}).click();await page.getByRole('button',{name:'Queue for Forge',exact:true}).waitFor();
assert.equal(await page.evaluate(()=>window.injected),undefined);assert.equal(writes.length,0);
await page.getByRole('button',{name:'Queue for Forge',exact:true}).click();await page.getByText('Queue for Forge?',{exact:true}).waitFor();assert.equal(writes.length,0);
await page.getByRole('button',{name:'Cancel',exact:true}).click();assert.equal(writes.length,0);
await page.getByRole('button',{name:'Queue for Forge',exact:true}).click();await page.locator('#confirm').click();await page.getByText('Applying your decision…',{exact:true}).waitFor();
assert.equal(writes[0].body.action,'queue');assert.equal(writes[0].body.revision,'current');assert(await page.getByRole('button',{name:'Queue for Forge',exact:true}).isDisabled());
await page.getByRole('button',{name:'Close issue'}).click();await page.getByLabel('Find an issue').fill('');await page.waitForFunction(()=>document.querySelectorAll('#rows tr').length===5);
await page.getByText('Finish the migration off legacy fields',{exact:true}).click();await page.getByRole('button',{name:'Release hold',exact:true}).click();
assert.equal(await page.locator('#reason').isVisible(),true);await page.locator('#confirm').click();assert.equal(writes.length,1);
await page.locator('#reason').fill('The migration is complete in every environment');fail=true;await page.locator('#confirm').click();await page.getByText('This issue changed. Refresh it before applying the decision.',{exact:true}).waitFor();
assert.equal(writes[1].body.action,'resume');await page.getByRole('button',{name:'Cancel',exact:true}).click();await page.getByRole('button',{name:'Close issue'}).click();
await page.screenshot({path:'/private/tmp/forge-issues-desktop.png',fullPage:true});
await page.getByText('Redact credential values at the logging boundary',{exact:true}).click();await page.getByRole('button',{name:'Recheck setup',exact:true}).waitFor();
assert.equal(await page.getByRole('button',{name:'Retry attempt',exact:true}).count(),0);
assert.equal(await page.getByRole('link',{name:'Repository setup →'}).getAttribute('href'),'/settings/repository?repo_id=1');
await page.screenshot({path:'/private/tmp/forge-issues-detail.png',fullPage:true});
for(const width of [320,390]){await page.setViewportSize({width,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));assert(await page.locator('#detail').evaluate(e=>e.scrollWidth<=e.clientWidth));}
await page.screenshot({path:'/private/tmp/forge-issues-mobile-detail.png',fullPage:true});await page.getByRole('button',{name:'Close issue'}).click();assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.screenshot({path:'/private/tmp/forge-issues-mobile.png',fullPage:true});
await page.getByLabel('Show',{exact:true}).selectOption('completed');await page.getByText('Repair old links',{exact:true}).waitFor();assert.equal(await page.locator('#rows tr').count(),1);
assert.deepEqual(errors,[]);console.log('Issues browser checks passed: observation, filters, decisions, stale conflicts, holds, setup, escaping, and mobile layout.');
}finally{await browser.close()}})().catch(e=>{console.error(e);process.exit(1)});
