'use strict';
(() => {
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels={ready:'Ready to enable',needs_setup:'Setup needed',unknown:'Check unavailable',unchecked:'Checking setup…',stale:'Check setup again',checking:'Checking setup…',access_needed:'App access needed'};
const $=id=>document.getElementById(id);
let snapshot=null,busy=false,enabling=false,downloaded=false,autoCheckKey='',refreshing=false,refreshStarted=null,refreshRequestedAt=0;
const repoParam=new URLSearchParams(location.search).get('repo_id');
const repoId=repoParam===null?null:Number(repoParam);
const roleNames={build:'Builder',review:'Reviewer',audit:'Auditor',controller:'Controller'};
function appAction(connection){
 const manage=connection.installation_url;
 if(/^https:\/\/github\.com\/(?:organizations\/[A-Za-z0-9_.-]+\/)?settings\/installations\/[0-9]+$/.test(manage||''))return `<a href="${esc(manage)}" target="_blank" rel="noopener noreferrer">Manage access ↗</a>`;
 if(/^https:\/\/github\.com\/apps\/[A-Za-z0-9-]+\/installations\/new$/.test(connection.install_url||''))return `<a href="${esc(connection.install_url)}" target="_blank" rel="noopener noreferrer">Install App ↗</a>`;
 return '<a href="/onboarding">Connect App →</a>';
}
function renderAccess(row){
 const complete=!!row?.access_ready;
 $('app-access').hidden=complete||(!row&&!$('missing-repository').open&&snapshot.repositories.some(r=>!r.enabled));
 $('app-access-description').textContent=row?'Give each App access to '+row.name+'. After making changes on GitHub, refresh access here.':'Use Manage access to add your repository to a Forge App, then refresh access here.';
 const accessLabels={missing_access:'Needs repository access',not_configured:'Not connected',missing_permissions:'Permissions need updating',unknown:'Refresh to check access',stale:'Refresh to check access',archived:'Repository is archived or disabled',name_changed:'Repository was renamed'};
 ForgeRefresh.html($('app-connections'),Object.entries(roleNames).map(([role,name])=>{
  const connection=snapshot.connections?.[role]||{},status=row?.access?.[role]||connection.status;
  const ready=row?status==='ready':false;
  return `<div class="app-row" data-key="${role}"><div><strong>${name}</strong>${ready?'':`<p>${esc(accessLabels[status]||'Choose the repository in GitHub’s access settings.')}</p>`}</div>${ready?'<span class="good">Connected ✓</span>':appAction(connection)}</div>`;
 }).join(''));
 ForgeRefresh.button($('check-access'),refreshing||snapshot.sync?.status==='running');
}
function renderPicker(){
 const query=$('repo-search').value.trim().toLowerCase();
 const rows=snapshot.repositories.filter(r=>!r.enabled&&r.name.toLowerCase().includes(query));
 ForgeRefresh.html($('repo-options'),rows.map(r=>`<li data-key="${r.id}"><a href="/settings/repository?repo_id=${r.id}">${esc(r.name)}</a></li>`).join(''));
 $('no-repositories').hidden=!!rows.length;
 if(!rows.length)$('app-access').hidden=false;
}
function renderProgress(row){
 const displayStatus=row?.protection_status==='checking'?(row.protection_previous_status||'checking'):row?.protection_status;
 const stage=!row?0:!row.access_ready?1:displayStatus!=='ready'?2:row.enabled&&snapshot.activation_available?4:3;
 $('progress-enable').textContent=snapshot.activation_available?'Enable':'Finish setup';
 ['repo','access','rules','enable'].forEach((name,index)=>{const element=$('progress-'+name);element.className=index<stage?'done':'';if(index===stage)element.setAttribute('aria-current','step');else element.removeAttribute('aria-current')});
}
function render(){
 if(!snapshot)return;
 const selected=repoId!==null;
 $('picker').hidden=selected;$('repository-workspace').hidden=!selected;$('repository-summary').hidden=!selected;
 renderAccess(selected?snapshot.repositories[0]:null);renderProgress(selected?snapshot.repositories[0]:null);
 if(!selected){renderPicker();return;}
 const row=snapshot.repositories[0],status=row.protection_status,checking=status==='checking',displayStatus=checking?(row.protection_previous_status||status):status,report=(row.protection?.available?row.protection:row.protection_last_known||row.protection)||{},ready=status==='ready',displayReady=displayStatus==='ready',prepared=['ready','needs_setup'].includes(displayStatus);
 const live=snapshot.activation_available,canEnable=live&&!row.enabled;
 $('page-title').textContent=live?(row.enabled?'Repository setup':'Enable repository'):'Prepare your repository';
 $('repository-name').textContent=row.name;
 $('branch').textContent=report.branch?'Default branch · '+report.branch:'Repository review settings';
 $('status').textContent=displayReady?(live?(row.enabled?'Enabled':'Ready to enable'):'Ready to continue'):(labels[displayStatus]||labels.unknown);
 if(checking&&row.protection_previous_status)$('status').textContent+=' · Refreshing…';
 $('status').className='status '+(displayReady?'good':'');
 $('description').textContent=displayReady?(canEnable?'Setup is complete. Enable automation to let Forge pick up eligible issues in this repository.':live?'Setup is complete and automation is enabled. No action is needed here.':'Repository setup is complete. Continue to finish setting up Forge.'):status==='needs_setup'?'Update the GitHub review rules below, then check again.':status==='access_needed'?'Connect the missing Apps before continuing.':'Forge checks App access and GitHub review rules before enabling automation.';
 const requirements=(report.requirements||[]).filter(r=>r.id!=='readable');
 const requirementHTML=r=>`<li><span class="check ${r.passed?'good':''}" aria-hidden="true">${r.passed?'✓':'○'}</span><div><strong>${esc(r.label)}</strong><p>${esc(r.detail)}</p></div></li>`;
 ForgeRefresh.html($('requirements'),requirements.filter(r=>!r.passed).map(requirementHTML).join(''));
 $('requirements').hidden=displayStatus!=='needs_setup'||!requirements.some(r=>!r.passed);
 ForgeRefresh.html($('verified-requirements'),requirements.map(requirementHTML).join(''));
 const existing=report.edit_url;
 $('steps').hidden=displayReady||!prepared;
 $('download').download='forge-review-rules-'+row.id+'.json';
 $('download-step').hidden=!!existing;
 $('download').href='/api/repositories/ruleset?repo_id='+row.id+'&revision='+encodeURIComponent(snapshot.revision);
 $('download').className='button '+(!downloaded&&!existing?'primary':'');
 $('download').textContent=downloaded?'Download again':'Download ruleset';
 $('import-title').textContent=existing?'Update the existing ruleset':'Import on GitHub';
 $('import-description').textContent=existing?'A Forge review ruleset already exists. Update its review settings using the requirements above.':'On GitHub, choose New ruleset → Import a ruleset. Select the file, review the settings and save.';
 $('github').href=existing||row.rules_url;
 $('github').textContent=existing?'Edit ruleset on GitHub ↗':'Open GitHub settings ↗';
 $('github').className='button '+(existing||downloaded?'primary':'');
 $('scope').textContent='Applies to every pull request targeting '+(report.branch||'the default branch')+'. Existing organization rules and stronger protections still apply.';
 $('check').className='button';
 ForgeRefresh.button($('check'),checking||busy,status==='access_needed');
 $('hint').textContent=status==='access_needed'?'Finish connecting the Apps above to continue.':status==='stale'?'Check again to confirm the current GitHub settings before continuing.':status==='unknown'?((row.protection?.errors?.[0]?.detail||'GitHub could not be reached. Try checking again.')+(row.protection_last_known?' Showing the last checked requirements.':'')):checking?'This usually takes a few seconds. Automation stays unchanged.':'';
 $('repository-workspace').hidden=status==='access_needed';
 $('check').hidden=status==='access_needed';
 $('enable').hidden=!canEnable||!displayReady;
 $('enable').disabled=busy||!ready;
 $('enable').textContent=enabling?'Enabling…':'Enable automation';
 $('continue').hidden=canEnable||!displayReady;
 $('continue').href=live?'/settings/github':snapshot.continue_url||'/onboarding';
 $('continue').textContent=live?'Back to repositories →':'Continue to Forge →';
 $('continue').setAttribute('aria-disabled',String(!ready||busy));if(!ready||busy)$('continue').removeAttribute('href');
 const canDownload=['ready','needs_setup'].includes(status)&&!busy;
 $('download').setAttribute('aria-disabled',String(!canDownload));if(!canDownload)$('download').removeAttribute('href');
 ForgeRefresh.html($('sources'),(report.sources||[]).map(s=>`<li><a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.name)}${s.kind==='ruleset'?' · ruleset':''} ↗</a></li>`).join(''));
 $('other-rules').textContent=(report.additional_rules||[]).length?'GitHub also enforces '+[...new Set(report.additional_rules.map(r=>r.type.replaceAll('_',' ')))].join(', ')+'.':'GitHub applies any additional repository protections at merge time.';
 $('checked-at').textContent=report.observed_at?'Last checked '+new Date(report.observed_at*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}):'';
}
const poller=ForgeRefresh.poll({interval:3000,allowed:()=>!busy,
 load:async()=>{
  const response=await fetch('/api/repositories'+(repoId===null?'':'?repo_id='+repoId),{cache:'no-store',signal:AbortSignal.timeout(30000)}),value=await response.json();
  if(!response.ok||!value.ok||!Array.isArray(value.repositories)||(repoId!==null&&!value.repositories.length))throw Error(value.reason||'Repository setup is unavailable. Return to GitHub access and refresh.');
  return value;
 },changed:value=>{
 if(refreshing&&(value.sync?.status==='interrupted'||Date.now()-refreshRequestedAt>90000)){refreshing=false;$('access-result').textContent='The access check did not finish. Refresh access to try again.';render();}
 if(refreshing&&value.sync?.status==='complete'&&value.sync.finished_at!==refreshStarted){refreshing=false;$('access-result').textContent='Access refreshed.';}
 snapshot=value;render();
 const row=repoId===null?null:value.repositories[0];
 const key=[value.revision,row?.protection_status,row?.protection?.observed_at].join(':');
 if(!busy&&row?.access_ready&&['unchecked','stale'].includes(row.protection_status)&&autoCheckKey!==key){autoCheckKey=key;void check();}
 },recovered:()=>{if($('error').textContent.startsWith('Could not refresh.'))$('error').textContent='';},
 failed:(error,previous)=>{$('error').textContent=(previous?'Could not refresh. Showing the last received data. ':'')+error.message;}
});
const load=()=>poller.refresh();
async function check(){
 if(busy||!snapshot)return;busy=true;$('error').textContent='';render();
 try{const response=await fetch('/api/repositories/check',{method:'POST',headers:{'Content-Type':'application/json','X-Forge-Action':snapshot.controls_token},signal:AbortSignal.timeout(30000),body:JSON.stringify({repo_id:repoId,revision:snapshot.revision})});const result=await response.json();if(!response.ok||!result.ok)throw Error(result.reason||'Could not start the check.');busy=false;await load();}
 catch(error){$('error').textContent=error.message;}finally{busy=false;render();}
}
async function enable(){
 if(busy||!snapshot||snapshot.repositories[0].protection_status!=='ready')return;
 busy=true;enabling=true;$('error').textContent='';render();
 try{
  const response=await fetch('/api/repositories/enable',{method:'POST',headers:{'Content-Type':'application/json','X-Forge-Action':snapshot.controls_token},signal:AbortSignal.timeout(30000),body:JSON.stringify({repo_id:repoId,revision:snapshot.revision})});
  const result=await response.json();if(!response.ok||!result.ok)throw Error(result.reason||'Could not enable this repository.');
  snapshot=result;render();$('continue').focus();
 }catch(error){$('error').textContent=error.message;busy=false;await load();}
 finally{busy=false;enabling=false;render()}
}
$('check').onclick=check;
$('enable').onclick=enable;
$('repo-search').oninput=()=>{if(snapshot){renderAccess(null);renderPicker()}};
$('missing-repository').ontoggle=()=>{if(snapshot&&repoId===null){renderAccess(null);renderPicker()}};
$('check-access').onclick=async()=>{
 if(refreshing||!snapshot)return;
 refreshing=true;refreshRequestedAt=Date.now();refreshStarted=snapshot.sync?.finished_at;$('error').textContent='';$('access-result').textContent='Checking App access…';render();
 try{
  const live=snapshot.activation_available;
  const response=await fetch(live?'/api/settings/sync':'/api/onboarding',{method:'POST',headers:{'Content-Type':'application/json','X-Forge-Action':snapshot.controls_token},body:JSON.stringify(live?{}:{action:'refresh'}),signal:AbortSignal.timeout(30000)});
  const result=await response.json();if(!response.ok||!result.ok)throw Error(result.reason||'Could not refresh access. Try again.');
  await load();
 }catch(error){refreshing=false;$('access-result').textContent='';$('error').textContent=error.message;render();}
};
$('download').onclick=()=>{downloaded=true;setTimeout(render,0);};
load().catch(e=>$('error').textContent=e.message);

})();
