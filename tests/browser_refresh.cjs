const renderPage=require('./browser_page.cjs');
const {chromium}=require(process.env.FORGE_PLAYWRIGHT_MODULE||'playwright-core');
const assert=require('assert');
(async()=>{const browser=await chromium.launch({headless:true,chromiumSandbox:true,executablePath:process.env.FORGE_BROWSER_PATH});try{
 const page=await browser.newPage();
 await page.route('http://forge.test/**',route=>route.fulfill({json:{ok:true,active:false}}));
 await page.goto('http://forge.test/');
 await page.setContent(renderPage('settings.html','/settings'));
 const result=await page.evaluate(async()=>{
  let release,calls=0,changed=0,fail=false,previous,recovered=0;
  const pending=new Promise(resolve=>release=resolve);
  const poller=ForgeRefresh.poll({interval:60000,load:async()=>{calls++;if(calls===1)await pending;if(fail)throw Error('offline');return {count:3};},changed:()=>changed++,failed:(e,value)=>previous=value,recovered:()=>recovered++});
  const a=poller.refresh(),b=poller.refresh();release();await Promise.all([a,b]);const coalesced=calls===1;
  fail=true;await poller.refresh();const retained=previous.count===3;fail=false;await poller.refresh();
  Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'));
  const before=calls;await poller.refresh();const paused=before===calls;
  Object.defineProperty(document,'hidden',{configurable:true,value:false});document.dispatchEvent(new Event('visibilitychange'));await poller.refresh();
  return {coalesced,retained,paused,changed,recovered};
 });
 assert.deepEqual(result,{coalesced:true,retained:true,paused:true,changed:1,recovered:1});
 console.log('Refresh lifecycle checks passed: coalescing, retained data on failure, unchanged snapshots, recovery and hidden tabs.');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1)});
