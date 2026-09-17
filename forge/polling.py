"""Shared browser refresh lifecycle and in-place rendering for operator views."""
SCRIPT = r'''<script>
window.ForgeRefresh = (() => {
  function poll({load, changed, failed, recovered, interval=5000, allowed=()=>true}) {
    let timer, pending, value, signature, hadError=false;
    function schedule(delay=interval){clearTimeout(timer);if(!document.hidden)timer=setTimeout(refresh,delay);}
    function refresh(){
      if(pending)return pending;
      if(document.hidden||!allowed()){schedule();return Promise.resolve(value);}
      pending=(async()=>{
        try{const next=await load(),key=JSON.stringify(next);value=next;if(hadError){hadError=false;recovered?.();}if(key!==signature){signature=key;changed(next);}schedule();return value;}
        catch(error){hadError=true;failed?.(error,value);schedule(Math.max(interval,15000));return value;}
        finally{pending=null;}
      })();return pending;
    }
    document.addEventListener('visibilitychange',()=>{clearTimeout(timer);if(!document.hidden)refresh();});
    return {refresh,start:refresh};
  }
  // Keep existing nodes, focus, selection and open details when data changes.
  function html(parent, markup){
    const template=document.createElement('template');template.innerHTML=markup;
    function patch(target, source){
      if(target.nodeType!==source.nodeType||target.nodeName!==source.nodeName){const replacement=source.cloneNode(true);target.replaceWith(replacement);return replacement;}
      if(target.nodeType===Node.TEXT_NODE){if(target.data!==source.data)target.data=source.data;return;}
      if(target.nodeType!==Node.ELEMENT_NODE)return;
      for(const attr of [...target.attributes])if(!source.hasAttribute(attr.name))target.removeAttribute(attr.name);
      for(const attr of source.attributes)if(target.getAttribute(attr.name)!==attr.value)target.setAttribute(attr.name,attr.value);
      children(target,source);
    }
    function children(target,source){
      const keyed=new Map([...target.children].filter(n=>n.dataset.key).map(n=>[n.dataset.key,n]));
      let cursor=target.firstChild;
      for(const desired of source.childNodes){
        const key=desired.nodeType===1&&desired.dataset.key;
        let node=key?keyed.get(key):cursor;
        if(!node){node=desired.cloneNode(true);target.insertBefore(node,cursor);}
        else{if(node!==cursor)target.insertBefore(node,cursor);node=patch(node,desired)||node;}
        cursor=node.nextSibling;
      }
      while(cursor){const next=cursor.nextSibling;cursor.remove();cursor=next;}
    }
    children(parent,template.content);
  }
  function button(element,working,disabled=false){
    if(element.classList.contains('icon-button')){
      element.classList.toggle('forge-refresh-active',working);element.disabled=working||disabled;element.setAttribute('aria-busy',String(working));return;
    }
    element.classList.add('forge-refresh-control');
    element.innerHTML='<span class="forge-refresh-spinner" aria-hidden="true"'+(working?'':' hidden')+'></span><span>'+(working?'Refreshing…':'Refresh')+'</span>';
    element.disabled=working||disabled;element.setAttribute('aria-busy',String(working));
  }
  return {poll,html,button};
})();
</script>'''
STYLE = '''<style>
.forge-refresh-control{min-width:120px}.forge-refresh-spinner{display:inline-block;width:12px;height:12px;border:1.5px solid currentColor;border-right-color:transparent;border-radius:50%;animation:forge-refresh-turn .8s linear infinite;vertical-align:middle;margin-right:7px}.forge-refresh-spinner[hidden]{display:none}
.forge-refresh-active svg{animation:forge-refresh-turn .8s linear infinite}
@keyframes forge-refresh-turn{to{transform:rotate(360deg)}}
@media(prefers-reduced-motion:reduce){.forge-refresh-spinner,.forge-refresh-active svg{animation:none}}
</style>'''
