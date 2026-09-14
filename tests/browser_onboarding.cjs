const {chromium}=require(process.env.FORGE_PLAYWRIGHT_MODULE||'playwright-core');
const fs=require('fs'),path=require('path'),assert=require('assert');
const {execFileSync}=require('child_process');
// Exercise the production manifest in a real browser form submission. The
// installation redirect fixture alone does not cover first-time registration.
const registration=JSON.parse(execFileSync('python3',['-c',`import json,tempfile
from forge.onboarding import Onboarding
with tempfile.TemporaryDirectory() as root:
 print(json.dumps(Onboarding(root).begin('build','example','organization','http://forge.test','synthetic-browser-session')))
`],{cwd:path.join(__dirname,'..'),encoding:'utf8'}));
(async()=>{const browser=await chromium.launch({headless:true,executablePath:process.env.FORGE_BROWSER_PATH});try{
const page=await browser.newPage({viewport:{width:1440,height:1080}}),errors=[],actions=[];
let registering=true,manifestSubmitted=false;
await page.route('https://github.com/**',async route=>{
const request=route.request();assert.equal(request.url(),registration.action);assert.equal(request.method(),'POST');
assert(request.headers()['content-type'].startsWith('application/x-www-form-urlencoded'));
const submitted=JSON.parse(new URLSearchParams(request.postData()).get('manifest'));
assert.deepEqual(submitted,registration.manifest);assert(submitted.url);assert(submitted.hook_attributes.url);assert.equal(submitted.hook_attributes.active,false);
manifestSubmitted=true;return route.fulfill({contentType:'text/html',body:'<h1>GitHub registration fixture</h1>'});});
page.on('pageerror',e=>errors.push(e.message));
const roleNames=['build','review','audit','controller'];
let state={ok:true,phase:'setup',generation:'first',revision:'config-one',roles:Object.fromEntries(roleNames.map(r=>[r,{status:'not_configured',registered:false}])),repositories:[],selected:null,owner:'',provider_connected:false,checks:{},can_start:false,operation:null,error:null,controls_token:'synthetic-browser-token'};
await page.route('http://forge.test/**',async route=>{const request=route.request(),url=new URL(request.url());
if(url.pathname==='/api/onboarding'&&request.method()==='GET')return route.fulfill({json:state});
if(url.pathname==='/api/onboarding'){
assert.equal(request.headers()['x-forge-action'],'synthetic-browser-token');const action=request.postDataJSON();actions.push(action);
if(action.action==='github'){assert.equal(action.owner,'example');return route.fulfill({json:{ok:true,...(registering?registration:{install_url:'http://forge.test/install'})}});}
if(action.action==='repository'){assert.equal(action.repo_id,10);state.selected={id:10,name:'example/project'};state.revision='selected';}
if(action.action==='provider'){assert.equal(action.api_key,null);state.provider_connected=true;}
if(action.action==='check'){state.operation={action:'check',step:'probe'};state.checks={github:'passed',provider:'checking',runner:'checking'};}
if(action.action==='start'){assert(state.can_start);state.phase='active';state.operation={action:'start'};}
if(action.action==='reset'){state.phase='resetting';state.operation={action:'reset',step:'stop_workers'};}
return route.fulfill({json:{ok:true}});}
if(url.pathname==='/factory')return route.fulfill({contentType:'text/html',body:'<h1>Actual factory dashboard</h1>'});
return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(__dirname,'../legacy/onboarding.html'),'utf8')});});
await page.goto('http://forge.test/onboarding');await page.getByRole('heading',{name:'Connect your GitHub workspace'}).waitFor();
await page.locator('#owner').fill('example');await page.getByRole('button',{name:'Connect Builder',exact:true}).click();await page.waitForURL(registration.action);assert(manifestSubmitted);
registering=false;await page.goto('http://forge.test/onboarding');await page.getByRole('heading',{name:'Connect your GitHub workspace'}).waitFor();
await page.locator('#owner').fill('example');await page.getByRole('button',{name:'Connect Builder',exact:true}).click();await page.waitForURL('**/install');
state.owner='example';state.roles=Object.fromEntries(roleNames.map(r=>[r,{status:'ok',registered:true}]));state.repositories=[{id:10,name:'example/project',access_ready:true}];
await page.goto('http://forge.test/onboarding');await page.getByRole('heading',{name:'Where should Forge work?'}).waitFor();await page.getByRole('radio').check();await page.getByRole('button',{name:'Continue',exact:true}).click();
await page.getByRole('heading',{name:'Ready to put Forge to work?'}).waitFor();assert(await page.getByRole('button',{name:'Start Forge',exact:true}).isDisabled());
await page.getByRole('button',{name:'Connect',exact:true}).click();await page.getByRole('dialog').waitFor();await page.getByRole('button',{name:'Connect and check',exact:true}).click();
await page.getByText('Checking setup…',{exact:false}).waitFor();assert(await page.getByRole('button',{name:'Start Forge',exact:true}).isDisabled());
state.operation=null;state.checks={github:'passed',provider:'passed',runner:'passed'};state.can_start=true;
await page.getByText('Everything is ready.',{exact:true}).waitFor();await page.screenshot({path:'/tmp/forge-onboarding-production-start.png',fullPage:true});
await page.getByRole('button',{name:'Start Forge',exact:true}).click();await page.waitForURL('**/factory');
await page.goto('http://forge.test/reset');await page.getByRole('heading',{name:'Start fresh',exact:true}).waitFor();
const prior=actions.length;await page.getByRole('link',{name:'Cancel',exact:true}).click();assert.equal(actions.length,prior);
await page.goto('http://forge.test/reset');await page.getByRole('button',{name:'Stop and reset Forge'}).click();await page.getByRole('heading',{name:'Resetting Forge'}).waitFor();
state.error='Engine unavailable. Cleanup has not been verified.';await page.reload();await page.getByText(state.error).waitFor();assert.equal(await page.getByRole('link',{name:'Set up Forge →'}).count(),0);
state.phase='setup';state.operation=null;state.error=null;state.generation='second';state.reset_receipt={id:'reset-one'};state.roles=Object.fromEntries(roleNames.map(r=>[r,{status:'not_configured',registered:false}]));state.selected=null;state.provider_connected=false;state.owner='';state.repositories=[];state.can_start=false;
await page.getByRole('heading',{name:'Forge has been reset'}).waitFor();await page.reload();await page.getByRole('heading',{name:'Forge has been reset'}).waitFor();
await page.getByRole('link',{name:'Set up Forge →'}).click();await page.getByRole('heading',{name:'Connect your GitHub workspace'}).waitFor();
await page.screenshot({path:'/tmp/forge-onboarding-production-github.png',fullPage:true});
await page.setViewportSize({width:390,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.screenshot({path:'/tmp/forge-onboarding-production-mobile.png',fullPage:true});
assert.deepEqual(errors,[]);assert.equal(actions.filter(a=>a.action==='start').length,1);assert.equal(actions.filter(a=>a.action==='reset').length,1);
console.log('Production onboarding browser checks passed: API actions, readiness gate, activation, reset failure/reload/completion, responsive layout.');
}finally{await browser.close()}})().catch(e=>{console.error(e);process.exit(1)});
