const {chromium} = require(process.env.FORGE_PLAYWRIGHT_MODULE || 'playwright-core');
const fs = require('fs'), assert = require('assert'), path = require('path');
(async()=>{
  const browser = await chromium.launch({headless:true,executablePath:process.env.FORGE_BROWSER_PATH,args:['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1000}}), errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    const roles=['build','review','audit','controller'];
    let config={version:1,sync_interval_seconds:300,access_max_age_seconds:900,connections:{},repositories:[]};
    let synced=false, failSync=false, revision=1, saves=0;
    const view=()=>({ok:true,controls_token:'synthetic',revision:String(revision),config:structuredClone(config),
      roles:Object.fromEntries(roles.map(role=>[role,{status:!config.connections[role]?'not_configured':failSync?'unknown':synced?'ok':'unknown',required_permissions:{contents:role==='build'?'write':'read'},missing_permissions:[],reason:failSync?'Could not reach GitHub':null}])),
      sync:synced?{status:'complete',finished_at:Date.now()/1000}:{status:'never'},
      repositories:synced?[{id:10,name:'example/project',enabled:config.repositories[0]?.enabled||false,access_ready:!failSync,access:Object.fromEntries(roles.map(r=>[r,failSync?'unknown':'ready']))}]:[]});
    await page.route('http://forge.test/**',async route=>{
      const req=route.request(),url=new URL(req.url());
      if(url.pathname==='/settings')return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(__dirname,'../legacy/settings.html'),'utf8')});
      if(url.pathname==='/api/factory')return route.fulfill({json:{ok:true,runtime:{version:1,enabled:false},runtime_revision:'runtime-one'}});
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
    await page.goto('http://forge.test/settings');
    await page.getByText('Save your App connections, then refresh access to discover repositories.').waitFor();
    for(const [i,role] of roles.entries()){
      await page.locator(`#${role}-app`).fill(String(i+1));
      await page.locator(`#${role}-installation`).fill(String(i+101));
      await page.locator(`#${role}-key`).fill(`FORGE_${role.toUpperCase()}_KEY_FILE`);
    }
    await page.getByRole('button',{name:'Save connections',exact:true}).click();
    await page.getByText('Settings saved.',{exact:true}).waitFor();assert.equal(saves,1);
    await page.getByRole('button',{name:'Refresh access',exact:true}).click();
    await page.getByRole('button',{name:'Enable',exact:true}).waitFor();
    await page.getByRole('button',{name:'Enable',exact:true}).click();
    await page.getByRole('dialog').waitFor();assert.equal(config.repositories.length,0);
    await page.getByRole('button',{name:'Cancel',exact:true}).click();assert.equal(config.repositories.length,0);
    await page.getByRole('button',{name:'Enable',exact:true}).click();
    await page.getByRole('button',{name:'Enable automation',exact:true}).click();
    await page.getByRole('button',{name:'Disable',exact:true}).waitFor();assert.equal(config.repositories[0].enabled,true);
    failSync=true;
    await page.getByRole('button',{name:'Refresh access',exact:true}).click();
    await page.getByText('Paused · access needs attention').waitFor();assert.equal(config.repositories[0].enabled,true);
    await page.locator('#sync-interval').fill('600');revision++; // External agent edits the file.
    await page.getByRole('button',{name:'Save sync settings',exact:true}).click();
    await page.getByText('Settings changed; reload before saving').waitFor();
    assert.equal(await page.locator('#sync-interval').inputValue(),'600');
    await page.screenshot({path:process.env.FORGE_SETTINGS_SCREENSHOT||'/tmp/forge-settings-desktop.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
    await page.screenshot({path:'/tmp/forge-settings-mobile.png',fullPage:true});
    assert.deepEqual(errors,[]);
    console.log('Settings browser checks passed: setup, sync, activation confirmation, access loss, edit conflict, mobile layout.');
  } finally {await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
