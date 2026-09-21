const renderPage=require('./browser_page.cjs');
const {chromium}=require(process.env.FORGE_PLAYWRIGHT_MODULE||'playwright-core');
const fs=require('fs'),path=require('path'),assert=require('assert');
(async()=>{const browser=await chromium.launch({chromiumSandbox:true,headless:true,executablePath:process.env.FORGE_BROWSER_PATH});try{
const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],requests=[];
page.setDefaultTimeout(10000);
page.on('pageerror',e=>errors.push(e.message));
await page.route('http://forge.test/**',async route=>{const url=new URL(route.request().url());
 if(url.pathname==='/issues')return route.fulfill({contentType:'text/html',body:renderPage('issues.html',url.pathname)});
 if(url.pathname==='/onboarding')return route.fulfill({contentType:'text/html',body:renderPage('onboarding.html',url.pathname)});
 if(url.pathname.startsWith('/assets/'))return route.fulfill({contentType:'application/javascript',body:fs.readFileSync(path.join(__dirname,'../legacy',url.pathname.replace('/assets/','')),'utf8')});
 if(url.pathname==='/api/issues'){requests.push(url.search);return route.fulfill({json:{ok:true,controls_token:'synthetic',complete:true,fresh:true,actions:[],page:1,pages:1,
  repositories:[{id:1,name:'XRPL-Commons/academy',complete:true,fresh:true},{id:2,name:'XRPL-Commons/hackathon',complete:true,fresh:true}],issues:[]}});}
 if(url.pathname==='/api/factory')return route.fulfill({json:{ok:true,active:false}});
 if(url.pathname==='/api/onboarding')return route.fulfill({json:{ok:true,phase:'setup',generation:'g',roles:{},checks:{},repositories:[],selected:null,owner:'',revision:'r'}});
 return route.fulfill({status:404,body:'missing'});});

await page.goto('http://forge.test/issues');
const state=page.locator('#state'),wrap=page.locator('.select-menu:has(#state)'),trigger=wrap.locator('.select-menu__trigger'),panel=wrap.locator('.select-menu__panel');
await trigger.waitFor();
// Every native select is enhanced, keeps its value, and stays usable by scripts and automation.
assert.equal(await page.locator('select').count(),await page.locator('.select-menu__trigger').count());
assert.equal(await trigger.locator('.select-menu__value').textContent(),'Open issues');
assert.equal(await trigger.getAttribute('aria-haspopup'),'listbox');
assert.equal(await panel.isHidden(),true);
await page.getByLabel('Show',{exact:true}).selectOption('completed');
assert.equal(await trigger.locator('.select-menu__value').textContent(),'Completed');

// Mouse: open, pick an option, the native select changes and its change handler reloads the view.
const before=requests.length;
await trigger.click();
assert.equal(await panel.isVisible(),true);
assert.equal(await trigger.getAttribute('aria-expanded'),'true');
assert.equal(await panel.locator('[role=option]').count(),7);
assert.equal(await panel.locator('[role=option][aria-selected=true]').textContent(),'Completed');
assert.equal(await panel.locator('.select-menu__search').isVisible(),false,'short lists are not searchable');
await panel.getByRole('option',{name:'Working'}).click();
assert.equal(await state.inputValue(),'working');
assert.equal(await panel.isHidden(),true);
assert.ok(requests.length>before&&requests[requests.length-1].includes('state=working'),'change event reloaded issues');
assert.equal(await page.evaluate(()=>document.activeElement.className),'select-menu__trigger','focus returns to the trigger');

// Keyboard: arrows open and move, Enter selects, Escape closes without changing.
await page.keyboard.press('ArrowDown');
assert.equal(await panel.isVisible(),true);
await page.keyboard.press('ArrowDown');
await page.keyboard.press('Enter');
assert.equal(await state.inputValue(),'waiting');
await page.keyboard.press('ArrowUp');
await page.keyboard.press('ArrowUp');
await page.keyboard.press('Escape');
assert.equal(await panel.isHidden(),true);
assert.equal(await state.inputValue(),'waiting');

// Options rendered later by scripts appear in the menu (repositories load from the API).
const repositoryWrap=page.locator('.select-menu:has(#repository)');
await repositoryWrap.locator('.select-menu__trigger').click();
await repositoryWrap.locator('.select-menu__panel').getByRole('option',{name:'XRPL-Commons/hackathon'}).waitFor();
assert.equal(await repositoryWrap.locator('[role=option]').count(),3);
await repositoryWrap.locator('.select-menu__panel').getByRole('option',{name:'XRPL-Commons/hackathon'}).click();
assert.equal(await page.locator('#repository').inputValue(),'2');
assert.ok(requests[requests.length-1].includes('repo_id=2'));
await page.keyboard.press('Escape');

// Long lists get a search box that filters options, like USelectMenu's searchable mode.
await page.evaluate(()=>{const s=document.getElementById('state');for(const v of ['a','b'])s.append(new Option('Extra '+v,'extra-'+v));});
await trigger.click();
const search=panel.locator('.select-menu__search');
await search.waitFor({state:'visible'});
assert.equal(await page.evaluate(()=>document.activeElement.className),'select-menu__search');
await search.fill('wait');
assert.equal(await panel.locator('[role=option]:visible').count(),1);
assert.equal(await panel.locator('[role=option]:visible').textContent(),'Waiting');
await search.fill('zzz');
await panel.locator('.select-menu__empty').waitFor();
await search.fill('extra b');
await page.keyboard.press('Enter');
assert.equal(await state.inputValue(),'extra-b');
assert.equal(await panel.isHidden(),true);

// Clicking outside closes the menu; only one menu is open at a time.
await trigger.click();
await repositoryWrap.locator('.select-menu__trigger').click();
assert.equal(await panel.isHidden(),true);
assert.equal(await repositoryWrap.locator('.select-menu__panel').isVisible(),true);
await page.mouse.click(5,5);
assert.equal(await repositoryWrap.locator('.select-menu__panel').isHidden(),true);

// Phone width: the trigger fills its field and meets the touch target size.
await page.setViewportSize({width:390,height:800});
await page.goto('http://forge.test/issues');
await trigger.waitFor();
const box=await trigger.boundingBox();
assert.ok(box.height>=44,'touch height '+box.height);
assert.ok(box.width>100,'fills the field '+box.width);
assert.deepEqual(errors,[]);
console.log('select menu browser checks passed');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1);});
