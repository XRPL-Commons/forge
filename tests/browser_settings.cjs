const renderPage=require('./browser_page.cjs');
const {chromium} = require(process.env.FORGE_PLAYWRIGHT_MODULE || 'playwright-core');
const fs = require('fs'), assert = require('assert'), path = require('path');
(async()=>{
  const browser = await chromium.launch({chromiumSandbox:true,headless:true,executablePath:process.env.FORGE_BROWSER_PATH,args:[]});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1000}}), errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    const roles=['build','review','audit','controller'];
    let config={version:1,sync_interval_seconds:300,access_max_age_seconds:900,connections:Object.fromEntries(roles.map((role,i)=>[role,{app_id:i+1,installation_id:i+101,private_key_env:`FORGE_${role.toUpperCase()}_KEY_FILE`}])),repositories:[]};
    let synced=false, failSync=false, revision=1, saves=0;
    let setup={ok:true,phase:'active',roles:{build:{registered:true,app_id:1,install_url:'https://github.com/apps/forge-builder-example/installations/new'}}}, setupRefreshes=0;
    const originalConnections=structuredClone(config.connections);
    let runtime={version:1,enabled:true,slots:{build:2,review:1,audit:1,recovery:1},provider_pool:[{id:'claude',provider:'claude',model:'default',provider_auth_file_env:'PRIVATE_REFERENCE'}],image:'unchanged-image'}, runtimeRevision=1;
    const view=()=>({ok:true,controls_token:'synthetic',revision:String(revision),config:structuredClone(config),
      roles:Object.fromEntries(roles.map(role=>[role,{status:!config.connections[role]?'not_configured':failSync?'unknown':synced?'ok':'unknown',required_permissions:{contents:role==='build'?'write':'read'},missing_permissions:[],reason:failSync?'Could not reach GitHub':null,app_slug:config.connections[role]?'forge-'+role+'-example':null,installation_url:config.connections[role]?'https://github.com/organizations/example/settings/installations/'+config.connections[role].installation_id:null}])),
      sync:synced?{status:'complete',finished_at:Date.now()/1000}:{status:'never'},
      repositories:synced?[{id:10,name:'example/project',enabled:config.repositories[0]?.enabled||false,protection_ready:true,protection_status:'ready',access_ready:!failSync&&roles.every(r=>config.connections[r]),access:Object.fromEntries(roles.map(r=>[r,!config.connections[r]?'not_configured':failSync?'unknown':'ready']))}]:[]});
    await page.route('http://forge.test/**',async route=>{
      const req=route.request(),url=new URL(req.url());
      if(['/settings','/settings/advanced','/settings/github'].includes(url.pathname))return route.fulfill({contentType:'text/html',body:renderPage('settings.html',url.pathname)});
      if(url.pathname==='/settings/repository')return route.fulfill({contentType:'text/html',body:renderPage('repository-setup.html',url.pathname)});
      if(url.pathname==='/assets/repository-setup.js')return route.fulfill({contentType:'text/javascript',body:fs.readFileSync(path.join(__dirname,'../legacy/repository-setup.js'),'utf8')});
      if(url.pathname==='/api/repositories')return route.fulfill({json:{...view(),activation_available:true,continue_url:'/factory'}});
      if(url.pathname==='/api/repositories/enable'){assert.deepEqual(req.postDataJSON(),{repo_id:10,revision:String(revision)});config.repositories=[{id:10,name:'example/project',enabled:true}];revision++;saves++;return route.fulfill({json:{...view(),activation_available:true}});}
      if(url.pathname==='/api/onboarding'){
        if(req.method()==='POST'){assert.equal(req.headers()['x-forge-action'],'synthetic');assert.deepEqual(req.postDataJSON(),{action:'refresh'});setupRefreshes++;}
        return route.fulfill({json:setup});
      }
      if(url.pathname==='/api/factory')return route.fulfill({json:{ok:true,runtime:structuredClone(runtime),runtime_revision:String(runtimeRevision)}});
      if(url.pathname==='/api/runtime'){const body=req.postDataJSON();if(body.revision!==String(runtimeRevision))return route.fulfill({json:{ok:false,reason:'Worker limits changed; reload before saving'}});runtime=body.config;runtimeRevision++;return route.fulfill({json:{ok:true,runtime:structuredClone(runtime),runtime_revision:String(runtimeRevision)}});}
      if(url.pathname==='/api/settings/sync'){synced=true;return route.fulfill({json:{ok:true}})}
      if(url.pathname==='/api/settings'){
        if(req.method()==='POST'){
          assert.equal(req.headers()['x-forge-action'],'synthetic');const body=req.postDataJSON();
          if(body.revision!==String(revision))return route.fulfill({json:{ok:false,reason:'Settings changed; reload before saving'}});
          config=body.config;revision++;saves++;
        }
        return route.fulfill({json:view()});
      }
      return route.fulfill({status:404,body:''});
    });
    await page.goto('http://forge.test/settings/github');
    await page.getByText('forge-build-example',{exact:true}).waitFor();
    assert.equal(await page.locator('#connections input, #config-editor, #runtime-editor').count(),0);
    assert.equal(await page.getByRole('button',{name:'Save connections',exact:true}).count(),0);
    assert.equal(await page.getByText('FORGE_BUILD_KEY_FILE',{exact:false}).count(),0);
    await page.getByRole('button',{name:'Refresh GitHub access',exact:true}).click();
    await page.getByRole('link',{name:'Set up',exact:true}).waitFor();
    // Exercise navigation from the table: a delegated button handler must not
    // cancel ordinary link clicks or keyboard activation.
    const beforeNavigationSaves=saves;
    for(const keyboard of [false,true]){
      const link=page.getByRole('link',{name:'Set up',exact:true});
      if(keyboard){await link.focus();await page.keyboard.press('Enter')}else await link.click();
      await page.waitForURL('http://forge.test/settings/repository?repo_id=10',{timeout:5000});
      await page.getByRole('heading',{name:'Enable repository',exact:true}).waitFor();
      await page.getByText('Ready to enable',{exact:true}).waitFor();
      await page.getByRole('navigation',{name:'Settings navigation'}).getByRole('link',{name:'GitHub access',exact:true}).click();
      await page.getByRole('link',{name:'Set up',exact:true}).waitFor();
    }
    assert.equal(saves,beforeNavigationSaves,'Viewing setup must not change repository activation');
    const disabledRowHeight=(await page.locator('#repositories tr').boundingBox()).height;
    assert((await page.getByRole('link',{name:'Set up',exact:true}).boundingBox()).height<=32);
    await page.getByRole('link',{name:'Set up',exact:true}).click();
    await page.getByRole('button',{name:'Enable automation',exact:true}).waitFor();assert.equal(config.repositories.length,0);
    await page.getByRole('navigation',{name:'Settings navigation'}).getByRole('link',{name:'GitHub access',exact:true}).click();assert.equal(config.repositories.length,0);
    await page.getByRole('link',{name:'Set up',exact:true}).click();
    await page.getByRole('button',{name:'Enable automation',exact:true}).click();
    await page.getByRole('navigation',{name:'Settings navigation'}).getByRole('link',{name:'GitHub access',exact:true}).click();
    await page.getByRole('button',{name:'Disable',exact:true}).waitFor();assert.equal(config.repositories[0].enabled,true);
    assert.equal((await page.locator('#repositories tr').boundingBox()).height,disabledRowHeight,'Activation must not change normal row height');
    const repositoryNode=await page.locator('#repositories tr').elementHandle();
    const accessNode=await page.locator('.connection a').first().elementHandle();
    await page.locator('#repo-search').fill('project');await accessNode.focus();
    await page.waitForTimeout(5200);
    assert(await repositoryNode.evaluate(el=>el.isConnected),'Polling must update existing repository rows');
    assert(await accessNode.evaluate(el=>el===document.activeElement),'Polling must preserve link focus');
    assert.equal(await page.locator('#repo-search').inputValue(),'project');
    failSync=true;
    await page.getByRole('button',{name:'Refresh GitHub access',exact:true}).click();
    await page.getByText('Needs attention',{exact:true}).waitFor();assert.equal(config.repositories[0].enabled,true);
    await page.goto('http://forge.test/settings');
    await page.waitForFunction(()=>document.getElementById('worker-build').value==='2');
    assert(await page.locator('#worker-actions').isHidden());
    await page.locator('#worker-build').fill('3');assert(await page.locator('#worker-actions').isVisible());
    await page.locator('#worker-build').fill('2');assert(await page.locator('#worker-actions').isHidden());
    assert(await page.locator('#github-title').isHidden());assert(await page.locator('#reset-title').isVisible());
    await page.locator('#worker-build').fill('6');
    await page.getByRole('button',{name:'Save worker limits',exact:true}).click();
    await page.getByText('Worker limits saved.',{exact:false}).waitFor();
    assert(await page.locator('#worker-actions').isHidden());
    assert.equal(runtime.slots.build,6);assert.equal(runtime.slots.recovery,1);
    assert.equal(runtime.image,'unchanged-image');assert.equal(runtime.provider_pool[0].provider_auth_file_env,'PRIVATE_REFERENCE');
    assert.deepEqual(config.connections,originalConnections);
    await page.locator('#worker-build').fill('8');runtimeRevision++;
    await page.getByRole('button',{name:'Save worker limits',exact:true}).click();
    await page.getByText('Worker limits changed; reload before saving').waitFor();
    assert.equal(await page.locator('#worker-build').inputValue(),'8');assert.equal(runtime.slots.build,6);
    await page.getByRole('button',{name:'Discard changes',exact:true}).click();
    await page.getByText('Changes discarded.',{exact:true}).waitFor();
    assert.equal(await page.locator('#worker-build').inputValue(),'6');
    assert(await page.locator('#worker-actions').isHidden());
    for(const width of [320,390]){
      await page.setViewportSize({width,height:844});await page.locator('#worker-build').fill('7');
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      await page.getByRole('button',{name:'Discard changes',exact:true}).click();
      await page.waitForFunction(()=>document.getElementById('worker-actions').hidden);
    }
    await page.setViewportSize({width:1440,height:1000});
    failSync=false;await page.goto('http://forge.test/settings/github');await page.getByText('forge-build-example',{exact:true}).waitFor();
    await page.evaluate(()=>scrollTo(0,0));
    await page.screenshot({path:'/tmp/forge-settings-operator-desktop.png',fullPage:true});
    for(const width of [320,390,760]){
      await page.setViewportSize({width,height:844});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      assert(await page.locator('.table-wrap').evaluate(el=>el.scrollWidth<=el.clientWidth),'Repository actions must not require sideways scrolling');
      const row=page.locator('#repositories tr').first();
      await row.scrollIntoViewIfNeeded();
      for(const cell of await row.locator('td').all()){
        const box=await cell.boundingBox();assert(box.x>=0&&box.x+box.width<=width);
      }
      assert.equal(await row.locator('td').count(),2);
      assert((await row.getByRole('button',{name:'Disable',exact:true}).boundingBox()).height>=44);
    }
    await page.setViewportSize({width:390,height:844});
    await page.evaluate(()=>scrollTo(0,0));
    await page.screenshot({path:'/tmp/forge-settings-operator-mobile.png',fullPage:true});
    await page.goto('http://forge.test/settings/advanced');
    await page.getByRole('heading',{name:'General',exact:true}).waitFor();
    assert.equal(await page.locator('#connections input, #config-editor, #runtime-editor').count(),0);
    // The operator page is useful before any installation exists and must not
    // confuse saved registration or a draft selection with verified access.
    config.connections={};config.repositories=[];synced=false;failSync=false;setup.phase='setup';
    await page.goto('http://forge.test/settings/github');
    await page.getByRole('heading',{name:'GitHub access',exact:true}).waitFor();
    await page.getByText('Installation unverified',{exact:true}).waitFor();
    assert.equal(await page.getByRole('link',{name:'Install App ↗'}).getAttribute('href'),setup.roles.build.install_url);
    assert.equal(await page.locator('#build-key').count(),0);
    assert.equal(await page.locator('#save-connections').count(),0);assert(await page.locator('#runtime-title').isHidden());
    const beforeOperatorSaves=saves;
    await page.getByRole('button',{name:'Refresh GitHub access',exact:true}).click();
    await page.getByText('Access refreshed.',{exact:true}).waitFor();assert.equal(setupRefreshes,1);
    config.connections={build:{app_id:1,installation_id:101,private_key_env:'FORGE_BUILD_KEY_FILE'}};synced=true;
    await page.reload();await page.getByText('example/project',{exact:true}).waitFor();
    assert.equal(await page.getByRole('link',{name:'Continue setup →',exact:true}).count(),1);
    assert.equal(await page.locator('#repositories button').count(),0);
    assert.equal(await page.getByRole('link',{name:'Manage access ↗'}).getAttribute('href'),'https://github.com/organizations/example/settings/installations/101');
    await page.locator('#repo-search').fill('missing');assert(await page.locator('#repositories tr').isHidden());
    await page.locator('#repo-search').fill('PROJECT');assert(await page.locator('#repositories tr').isVisible());
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
    await page.screenshot({path:'/tmp/forge-github-access-mobile.png',fullPage:true});
    setup.phase='active';await page.reload();
    await page.getByRole('navigation',{name:'Settings navigation'}).getByRole('link',{name:'General',exact:true}).waitFor();
    assert(await page.getByRole('link',{name:'Set up',exact:true}).isVisible());
    assert.equal(saves,beforeOperatorSaves);
    await page.setViewportSize({width:1440,height:1000});await page.screenshot({path:'/tmp/forge-github-access-desktop.png',fullPage:true});
    assert.deepEqual(errors,[]);
    console.log('Settings browser checks passed: setup, sync, activation confirmation, access loss, edit conflict, operator App inventory, partial grants, search, mobile layout.');
  } finally {await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
