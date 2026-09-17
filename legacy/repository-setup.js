'use strict';
(() => {
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels={ready:'Review requirements verified',needs_setup:'Review settings need attention',unknown:'Could not check',unchecked:'Not checked yet',stale:'Check again to confirm',checking:'Checking GitHub',access_needed:'GitHub access needs attention'};
const $=id=>document.getElementById(id);
let snapshot=null,busy=false,downloaded=false;
const repoId=Number(new URLSearchParams(location.search).get('repo_id'));
function render(){
 if(!snapshot)return;
 const row=snapshot.repositories[0],status=row.protection_status,checking=status==='checking',displayStatus=checking?(row.protection_previous_status||status):status,report=(row.protection?.available?row.protection:row.protection_last_known||row.protection)||{},ready=displayStatus==='ready',prepared=['ready','needs_setup'].includes(displayStatus);
 $('repository-name').textContent=row.name;
 $('branch').textContent=report.branch?'Default branch · '+report.branch:'Repository review settings';
 $('status').textContent=(labels[displayStatus]||labels.unknown)+(checking&&row.protection_previous_status?' · Refreshing…':'');
 $('status').className='status '+(ready?'good':'');
 $('description').textContent=ready?'Your review requirements are in place. You can continue setup.':'Prepare the review rules once. You keep control of the changes on GitHub.';
 const requirements=(report.requirements||[]).filter(r=>r.id!=='readable');
 ForgeRefresh.html($('requirements'),requirements.map(r=>`<li><span class="check ${r.passed?'good':''}" aria-hidden="true">${r.passed?'✓':'○'}</span><div><strong>${esc(r.label)}</strong><p>${esc(r.detail)}</p></div></li>`).join(''));
 $('requirements').hidden=!requirements.length;
 const existing=report.edit_url;
 $('steps').hidden=ready||!prepared;
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
 ForgeRefresh.button($('check'),checking||busy);
 $('hint').textContent=status==='access_needed'?'All four Apps need access to this repository.':status==='stale'?'The last observation has expired. Your GitHub settings have not been changed.':status==='unknown'?((row.protection?.errors?.[0]?.detail||'GitHub could not be reached. Try checking again.')+(row.protection_last_known?' Showing the last checked requirements.':'')):checking?'This reads GitHub settings. It does not start workers or change your repository.':'';
 $('access-link').hidden=status!=='access_needed';
 $('continue').hidden=!ready;
 $('continue').setAttribute('aria-disabled',String(checking||busy));
 if(checking||busy)$('continue').removeAttribute('href');
 if(!checking&&!busy)$('continue').href=snapshot.continue_url||'/onboarding';
 $('download').setAttribute('aria-disabled',String(checking||busy));if(checking||busy)$('download').removeAttribute('href');
 ForgeRefresh.html($('sources'),(report.sources||[]).map(s=>`<li><a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.name)}${s.kind==='ruleset'?' · ruleset':''} ↗</a></li>`).join(''));
 $('other-rules').textContent=(report.additional_rules||[]).length?'GitHub also enforces '+[...new Set(report.additional_rules.map(r=>r.type.replaceAll('_',' ')))].join(', ')+'.':'GitHub applies any additional repository protections at merge time.';
 $('checked-at').textContent=report.observed_at?'Last checked '+new Date(report.observed_at*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}):'';
}
const poller=ForgeRefresh.poll({interval:3000,allowed:()=>!busy,
 load:async()=>{
  const response=await fetch('/api/repositories?repo_id='+repoId,{cache:'no-store',signal:AbortSignal.timeout(30000)}),value=await response.json();
  if(!response.ok||!value.ok||!value.repositories?.length)throw Error(value.reason||'Repository setup is unavailable.');
  return value;
 },changed:value=>{snapshot=value;render();},
 recovered:()=>{if($('error').textContent.startsWith('Could not refresh.'))$('error').textContent='';},
 failed:(error,previous)=>{$('error').textContent=(previous?'Could not refresh. Showing the last received data. ':'')+error.message;}
});
const load=()=>poller.refresh();
async function check(){
 if(busy||!snapshot)return;busy=true;$('error').textContent='';render();
 try{const response=await fetch('/api/repositories/check',{method:'POST',headers:{'Content-Type':'application/json','X-Forge-Action':snapshot.controls_token},signal:AbortSignal.timeout(30000),body:JSON.stringify({repo_id:repoId,revision:snapshot.revision})});const result=await response.json();if(!response.ok||!result.ok)throw Error(result.reason||'Could not start the check.');busy=false;await load();}
 catch(error){$('error').textContent=error.message;}finally{busy=false;render();}
}
$('check').onclick=check;
$('download').onclick=()=>{downloaded=true;setTimeout(render,0);};
load().then(()=>{if(snapshot?.repositories[0].protection_status==='unchecked')check();}).catch(e=>$('error').textContent=e.message);

})();
