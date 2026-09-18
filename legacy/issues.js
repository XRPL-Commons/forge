'use strict';
(()=>{
const $=id=>document.getElementById(id),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const names={backlog:'Backlog',queued:'Queued',working:'Working',attention:'Needs attention',completed:'Completed',waiting:'Waiting'};
const labels={queue:'Queue for Forge',backlog:'Return to backlog',pause:'Pause',resume:'Release hold',retry:'Retry attempt',recheck:'Recheck setup',stop:'Stop attempt'};
const effects={queue:'Forge will check repository setup, dependencies and existing work before starting when capacity is available.',backlog:'This removes the issue from Forge’s queue. The GitHub issue stays open.',pause:'Forge will leave this issue on hold until you choose to resume it.',resume:'This releases the deliberate hold. Confirm that the prerequisite work is complete. Forge will check dependencies and existing work before continuing.',retry:'This grants a new three-attempt budget and returns the issue to Build. Existing history is preserved.',recheck:'Forge will recheck the repository’s current access and review rules. A repaired setup hold can resume without resetting its failure budget.',stop:'This stops the active attempt and discards its unfinished workspace changes. Published pull requests are preserved.'};
let snapshot=null,selected=new URLSearchParams(location.search).get('issue'),detail=null,page=1,busy=false,decision=null,refreshing=false,loaded='',loadedKey=null,searchTimer,submitted=null;
function params(){const p=new URLSearchParams({state:$('state').value,page:String(page)});if($('repository').value)p.set('repo_id',$('repository').value);if($('search').value.trim())p.set('q',$('search').value.trim());return p.toString()}
async function api(path,body){const options={cache:'no-store',signal:AbortSignal.timeout(30000)};if(body!==undefined)Object.assign(options,{method:'POST',headers:{'Content-Type':'application/json','X-Forge-Action':snapshot.controls_token},body:JSON.stringify(body)});const response=await fetch(path,options),value=await response.json();if(!response.ok||!value.ok)throw Error(value.reason||'Could not load issues. Try refreshing.');return value}
function pending(key){return snapshot?.actions?.some(a=>a.key===key&&a.status==='pending')}
function rowAction(row){if(pending(row.key))return 'Applying…';if(row.status==='working')return 'View progress';if(row.status==='attention')return 'Review';if(row.status==='backlog'&&row.actions.includes('queue'))return 'Queue';return 'View issue'}
function render(){
 const focusedRepo=$('repository').value;
 ForgeRefresh.html($('repository'),'<option value="">All repositories</option>'+snapshot.repositories.map(r=>`<option value="${r.id}">${esc(r.name)}</option>`).join(''));
 $('repository').value=focusedRepo;
 ForgeRefresh.html($('rows'),snapshot.issues.map(row=>`<tr data-key="${esc(row.key)}"><td><span class="issue-meta">${esc(row.repo)} · #${row.number}</span><a class="issue-title" href="/issues?issue=${encodeURIComponent(row.key)}" data-open="${esc(row.key)}">${esc(row.title)}</a>${row.pull_requests.length?`<small>Pull request #${row.pull_requests[0].number}</small>`:''}</td><td><span class="state ${row.status}">${names[row.status]}</span><p class="reason">${esc(row.explanation)}</p></td><td><button class="row-action ${row.status==='backlog'&&row.actions.includes('queue')?'primary':''}" data-open="${esc(row.key)}">${rowAction(row)}</button></td></tr>`).join(''));
 $('empty').hidden=!!snapshot.issues.length;$('add-repository').hidden=!!snapshot.repositories.length;
 $('empty-title').textContent=!snapshot.repositories.length?'Choose where Forge should work':!snapshot.complete?'Loading the issue list':'No issues in this view';
 $('empty-description').textContent=!snapshot.repositories.length?'Add and enable a repository to see its issues here.':!snapshot.complete?'Previously received issues remain visible while Forge checks GitHub.':'Try another filter.';
 const problem=snapshot.repositories.find(r=>r.error);
 $('freshness').textContent=problem?problem.error:!snapshot.repositories.length?'':snapshot.complete?'Up to date · Viewing issues never queues them for work.':'Showing received issues. The GitHub inventory is still incomplete or needs refreshing.';
 $('freshness').classList.toggle('warning',!!problem);
 $('count').textContent=`${snapshot.total} ${snapshot.total===1?'issue':'issues'}${snapshot.complete?'':' received'} · Page ${snapshot.page} of ${snapshot.pages}`;
 $('previous').disabled=page<=1;$('next').disabled=page>=snapshot.pages;
 ForgeRefresh.button($('refresh'),refreshing,busy);
 if(detail&&selected===detail.key)renderDetail();
}
function renderDetail(){
 $('detail-meta').textContent=detail.repo+' · #'+detail.number;$('detail-title').textContent=detail.title;
 $('detail-state').textContent=names[detail.status];$('detail-state').className='state '+detail.status;
 $('detail-explanation').textContent=detail.explanation;
 ForgeRefresh.html($('detail-reviews'),(detail.reviews||[]).map(r=>`<p><strong>${r.role==='review'?'Review':'Security review'}: ${r.outcome==='pass'?'passed':'needs attention'}</strong>${r.summary?' · '+esc(r.summary):''}</p>`).join(''));
 $('detail-description').textContent=(detail.body||'No description has been added yet.')+(detail.body_truncated?'\n\nDescription shortened. Open on GitHub to read the full issue.':'');
 ForgeRefresh.html($('detail-links'),`<a href="https://github.com/${esc(detail.repo)}/issues/${detail.number}" target="_blank" rel="noopener noreferrer">Open on GitHub ↗</a>`+detail.pull_requests.map(p=>`<a href="https://github.com/${esc(detail.repo)}/pull/${p.number}" target="_blank" rel="noopener noreferrer">Pull request #${p.number} ↗</a>`).join(''));
 ForgeRefresh.html($('detail-dependencies'),detail.dependencies.length?'<p class="muted">Depends on '+detail.dependencies.map(n=>`<a href="/issues?issue=${detail.repo_id}%3A${n}" data-open="${detail.repo_id}:${n}">#${n}</a>`).join(', ')+'</p>':'');
 const actions=detail.actions||[];
 ForgeRefresh.html($('detail-actions'),actions.map((action,index)=>`<button data-action="${action}" class="${index===0?'primary':''}" ${busy||pending(detail.key)?'disabled':''}>${labels[action]}</button>`).join('')+(detail.setup_needed?`<a class="button ${actions.length?'':'primary'}" href="${esc(detail.setup_url)}">Repository setup →</a>`:'')+(detail.active&&detail.status==='working'?'<button data-action="stop">Stop attempt</button>':''));
 const last=(snapshot.actions||[]).filter(a=>a.key===detail.key).at(-1);
 if(last&&(!submitted||last.id===submitted))$('detail-message').textContent=last.status==='pending'?'Applying your decision…':last.message||'';
 if(!detail.fresh&&!last)$('detail-message').textContent='Showing the last received issue. Refresh before making a decision.';
 const events={discovered:'Added to Forge',held:'Put on hold',protection_paused:'Repository setup paused work',protection_hold_migrated:'Recognized an earlier setup hold',protection_restored:'Repository setup verified',human_retry:'Another attempt approved',operator_queue:'Queued for Forge',operator_backlog:'Returned to backlog',operator_pause:'Paused by an operator',operator_resume:'Hold released',operator_recheck:'Repository setup checked',operator_conflict:'Decision needs another review',reserved:'Attempt prepared',merged:'Pull request merged',cancelled:'Attempt stopped'};
 ForgeRefresh.html($('history'),detail.history.map(e=>`<li><time>${new Date(e.at*1000).toLocaleString()}</time>${esc(events[e.kind]||e.kind.replaceAll('_',' '))}${e.reason?`<p>${esc(e.reason)}</p>`:''}</li>`).join('')||'<li>No Forge activity yet.</li>');
}
const poller=ForgeRefresh.poll({interval:5000,allowed:()=>!busy&&!decision,
 load:async()=>{const query=params(),key=selected;const values=await Promise.all([api('/api/issues?'+query),key?api('/api/issues?issue='+encodeURIComponent(key)):Promise.resolve(null)]);return {query,key,list:values[0],detail:values[1]?.issue}},
 changed:value=>{if(value.query!==params()||value.key!==selected)return;snapshot=value.list;detail=value.detail;loaded=value.query;loadedKey=value.key;render();if(selected&&!$('detail').open)$('detail').showModal()},
 failed:error=>{(selected?$('detail-message'):$('freshness')).textContent=error.message},
 recovered:()=>{$('detail-message').textContent=''}
});
async function reload(){await poller.refresh();if(loaded!==params()||loadedKey!==selected)await poller.refresh()}
async function open(key){selected=key;detail=null;decision=null;submitted=null;$('decision').hidden=true;$('detail-title').textContent='Loading issue…';$('detail-message').textContent='';$('detail-reviews').replaceChildren();$('detail-description').textContent='';$('detail-actions').replaceChildren();if(!$('detail').open)$('detail').showModal();history.replaceState(null,'','/issues?issue='+encodeURIComponent(key));await reload()}
function close(){selected=null;detail=null;decision=null;submitted=null;$('decision').hidden=true;$('detail').close();history.replaceState(null,'','/issues');void reload()}
$('close').onclick=close;$('detail').addEventListener('cancel',event=>{event.preventDefault();close()});
document.addEventListener('click',event=>{const link=event.target.closest('[data-open]');if(link){event.preventDefault();void open(link.dataset.open);return}const button=event.target.closest('[data-action]');if(!button||!detail||busy)return;decision={action:button.dataset.action,key:detail.key,revision:detail.revision,active:detail.active};$('decision-title').textContent=labels[decision.action]+'?';$('decision-effect').textContent=effects[decision.action];const required=['pause','resume','retry','stop'].includes(decision.action);$('decision-label').hidden=!required;$('reason').hidden=!required;$('reason').value='';$('confirm').textContent=labels[decision.action];$('decision').hidden=false;(required?$('reason'):$('confirm')).focus()});
$('cancel').onclick=()=>{decision=null;$('decision').hidden=true;void reload()};
$('confirm').onclick=async()=>{if(!decision||busy)return;const reason=$('reason').hidden?labels[decision.action]+' confirmed by operator':$('reason').value.trim();if(!reason){$('reason').focus();return}busy=true;$('confirm').disabled=true;try{const result=decision.action==='stop'?await api('/api/factory/action',{action:'stop',target:decision.active,reason}):await api('/api/issues/action',{key:decision.key,action:decision.action,revision:decision.revision,reason});submitted=result.request;decision=null;$('decision').hidden=true;$('detail-message').textContent='Decision submitted. Waiting for confirmation.';}catch(error){$('detail-message').textContent=error.message}finally{busy=false;$('confirm').disabled=false;await reload()}};
$('refresh').onclick=async()=>{if(refreshing||!snapshot)return;refreshing=true;render();try{await api('/api/issues/refresh',{});await reload()}catch(error){$('freshness').textContent=error.message}finally{refreshing=false;render()}};
$('search').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{page=1;void reload()},200)};
$('repository').onchange=$('state').onchange=()=>{page=1;void reload()};$('previous').onclick=()=>{page--;void reload()};$('next').onclick=()=>{page++;void reload()};
void poller.start();
})();
