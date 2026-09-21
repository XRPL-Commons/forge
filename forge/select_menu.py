"""Shared select menu: every native <select> on an operator page becomes a searchable listbox.

The native element stays in the document as the source of truth (forms, scripts and
browser automation keep using it); the menu mirrors its options and value, like
Nuxt UI's USelectMenu does for a bound value.
"""

STYLE = '''<style>
.select-menu{position:relative;display:inline-flex;max-width:100%;min-width:0;vertical-align:middle}
.select-menu__native{position:absolute!important;left:0;top:0;width:1px!important;height:1px!important;min-height:0!important;min-width:0!important;opacity:0;pointer-events:none;margin:0!important;padding:0!important;border:0!important;overflow:hidden}
.select-menu__trigger{display:inline-flex;align-items:center;justify-content:space-between;gap:10px;width:100%;max-width:100%;box-sizing:border-box;min-height:42px;margin:0;padding:9px 12px;border:1px solid var(--line,#303b40);border-radius:8px;background:var(--panel,#192024);color:var(--text,#edf1ee);font:inherit;font-size:15px;line-height:1.4;text-align:left;cursor:pointer}
.select-menu__trigger:hover{border-color:#526269;background:var(--panel,#192024)}
.select-menu__trigger:focus-visible{outline:none;border-color:var(--blue,#abcfff);box-shadow:0 0 0 2px rgba(171,207,255,.35)}
.select-menu__trigger[aria-expanded=true]{border-color:var(--blue,#abcfff)}
.select-menu__trigger[aria-disabled=true]{opacity:.55;cursor:default}
.select-menu__text{display:grid;min-width:0;overflow:hidden}
.select-menu__text>*{grid-area:1/1}
.select-menu__value{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.select-menu__sizer{visibility:hidden;height:0;overflow:hidden;white-space:nowrap}
.select-menu__sizer span{display:block}
.select-menu__chevron{flex:none;color:var(--muted,#acb7b9);transition:transform .15s}
.select-menu__trigger[aria-expanded=true] .select-menu__chevron{transform:rotate(180deg)}
.select-menu__panel{position:absolute;z-index:20;top:calc(100% + 6px);left:0;box-sizing:border-box;min-width:100%;width:max-content;max-width:min(380px,calc(100vw - 32px));padding:6px;background:var(--panel,#192024);border:1px solid var(--line,#303b40);border-radius:10px;box-shadow:0 12px 32px rgba(0,0,0,.45);color:var(--text,#edf1ee)}
.select-menu__panel[hidden]{display:none}
.select-menu__panel.is-up{top:auto;bottom:calc(100% + 6px)}
.select-menu__panel.is-end{left:auto;right:0}
.select-menu__search{display:none;width:100%;box-sizing:border-box;margin:0 0 6px;min-height:36px;padding:7px 10px;border:1px solid var(--line,#303b40);border-radius:8px;background:var(--bg,#101416);color:inherit;font:inherit;font-size:14px}
.select-menu__search:focus{outline:none;border-color:var(--blue,#abcfff)}
.select-menu__panel.is-searchable .select-menu__search{display:block}
.select-menu__list{list-style:none;margin:0;padding:0;max-height:280px;overflow:auto;overscroll-behavior:contain}
.select-menu__option{display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:34px;padding:6px 10px;border-radius:7px;cursor:pointer;white-space:nowrap;font-size:14px}
.select-menu__option[aria-selected=true]{color:var(--green,#bfe6cd)}
.select-menu__option.is-active{background:#20292e}
.select-menu__option[aria-disabled=true]{opacity:.5;cursor:default}
.select-menu__check{flex:none;visibility:hidden}
.select-menu__option[aria-selected=true] .select-menu__check{visibility:visible}
.select-menu__empty{padding:8px 10px;color:var(--muted,#acb7b9);font-size:14px}
@media(max-width:700px){.select-menu{display:flex;width:100%}.select-menu__trigger{min-height:44px;font-size:16px}.select-menu__option{min-height:40px}}
@media(prefers-reduced-motion:reduce){.select-menu__chevron{transition:none}}
</style>'''

SCRIPT = r'''<script>(()=>{
  const CLASS='select-menu', menus=new WeakMap();
  let sequence=0, openMenu=null;
  const uid=()=>CLASS+'-'+(++sequence);
  const CHEVRON='<svg class="'+CLASS+'__chevron" viewBox="0 0 20 20" width="16" height="16" aria-hidden="true" focusable="false"><path d="M5.5 7.5 10 12l4.5-4.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const CHECK='<svg class="'+CLASS+'__check" viewBox="0 0 20 20" width="15" height="15" aria-hidden="true" focusable="false"><path d="M4.5 10.5 8.5 14l7-8" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  function fieldName(select){
    const explicit=select.getAttribute('aria-label');if(explicit)return explicit;
    const wrapping=select.closest('label');
    if(wrapping){const own=Array.from(wrapping.childNodes).filter(n=>n.nodeType===Node.TEXT_NODE).map(n=>n.textContent).join('').trim();if(own)return own;}
    if(select.id){const label=document.querySelector('label[for="'+CSS.escape(select.id)+'"]');if(label)return label.textContent.trim();}
    return 'Options';
  }
  function enhance(select){
    if(select.multiple||select.dataset.selectMenu==='off')return;
    const known=menus.get(select);
    if(known){if(select.parentNode===known.wrap)return;menus.delete(select);known.wrap.remove();}
    const wrap=document.createElement('span');wrap.className=CLASS;
    select.parentNode.insertBefore(wrap,select);wrap.appendChild(select);select.classList.add(CLASS+'__native');
    // Not a <button>: a wrapping <label> must keep pointing at the native select only.
    const trigger=document.createElement('div');trigger.className=CLASS+'__trigger';trigger.tabIndex=0;
    trigger.setAttribute('role','button');trigger.setAttribute('aria-haspopup','listbox');trigger.setAttribute('aria-expanded','false');
    const text=document.createElement('span');text.className=CLASS+'__text';
    const value=document.createElement('span');value.className=CLASS+'__value';value.id=uid();
    const sizer=document.createElement('span');sizer.className=CLASS+'__sizer';sizer.setAttribute('aria-hidden','true');
    text.append(value,sizer);trigger.append(text);trigger.insertAdjacentHTML('beforeend',CHEVRON);
    const panel=document.createElement('div');panel.className=CLASS+'__panel';panel.hidden=true;
    const search=document.createElement('input');search.type='search';search.className=CLASS+'__search';search.placeholder='Search…';search.autocomplete='off';search.setAttribute('aria-label','Search options');
    const list=document.createElement('ul');list.className=CLASS+'__list';list.id=uid();list.setAttribute('role','listbox');list.tabIndex=-1;
    trigger.setAttribute('aria-controls',list.id);
    panel.append(search,list);wrap.append(trigger,panel);
    const menu={select,wrap,trigger,value,sizer,panel,search,list,items:[],active:-1,query:''};
    menus.set(select,menu);
    trigger.addEventListener('click',event=>{event.preventDefault();if(trigger.getAttribute('aria-disabled')==='true')return;menu.panel.hidden?open(menu):close(menu);});
    trigger.addEventListener('keydown',event=>{
      if(trigger.getAttribute('aria-disabled')==='true')return;
      if(event.key==='Enter'||event.key===' '){event.preventDefault();menu.panel.hidden?open(menu):close(menu);}
      else if(['ArrowDown','ArrowUp'].includes(event.key)){event.preventDefault();if(menu.panel.hidden)open(menu);else move(menu,event.key==='ArrowDown'?1:-1);}
      else if(event.key==='Home'||event.key==='End'){event.preventDefault();open(menu);move(menu,event.key==='Home'?-Infinity:Infinity);}
      else if(event.key.length===1&&!event.altKey&&!event.ctrlKey&&!event.metaKey&&event.key!==' '){open(menu);if(menu.panel.classList.contains('is-searchable')){menu.search.value=event.key;filter(menu);event.preventDefault();}else typeahead(menu,event.key);}
    });
    panel.addEventListener('keydown',event=>{
      if(event.key==='ArrowDown'||event.key==='ArrowUp'){event.preventDefault();move(menu,event.key==='ArrowDown'?1:-1);}
      else if(event.key==='Home'||event.key==='End'){event.preventDefault();move(menu,event.key==='Home'?-Infinity:Infinity);}
      else if(event.key==='Enter'){event.preventDefault();if(menu.active>=0)choose(menu,menu.active);}
      else if(event.key==='Escape'){event.preventDefault();event.stopPropagation();close(menu,true);}
      else if(event.key==='Tab'){close(menu);}
      else if(event.target===menu.list&&event.key.length===1&&!event.altKey&&!event.ctrlKey&&!event.metaKey){typeahead(menu,event.key);}
    });
    search.addEventListener('input',()=>filter(menu));
    list.addEventListener('pointermove',event=>{const option=event.target.closest('.'+CLASS+'__option');if(option&&!option.hidden)activate(menu,Number(option.dataset.index),false);});
    list.addEventListener('click',event=>{const option=event.target.closest('.'+CLASS+'__option');if(option)choose(menu,Number(option.dataset.index));});
    list.addEventListener('mousedown',event=>event.preventDefault());
    wrap.addEventListener('focusout',event=>{if(!wrap.contains(event.relatedTarget))close(menu);});
    select.addEventListener('focus',()=>{if(select.matches(':focus-visible')||document.activeElement===select)trigger.focus();});
    select.addEventListener('change',()=>sync(menu));
    trigger.addEventListener('pointerdown',()=>sync(menu));
    trigger.addEventListener('focus',()=>sync(menu));
    new MutationObserver(()=>sync(menu)).observe(select,{childList:true,subtree:true,attributes:true,characterData:true});
    sync(menu);
  }
  function sync(menu){
    const {select,trigger,value,sizer,list}=menu;
    const options=Array.from(select.options);
    const selected=select.selectedOptions[0];
    const signature=JSON.stringify([select.disabled,options.map(o=>[o.value,o.textContent,o.disabled])]);
    value.textContent=selected?selected.textContent:'';
    trigger.setAttribute('aria-label',fieldName(select)+': '+value.textContent);
    trigger.setAttribute('aria-disabled',String(select.disabled));trigger.tabIndex=select.disabled?-1:0;
    if(signature!==menu.signature){
      menu.signature=signature;
      sizer.replaceChildren(...options.map(o=>{const span=document.createElement('span');span.textContent=o.textContent;return span;}));
      menu.items=options.map((option,index)=>{
        const item=document.createElement('li');item.className=CLASS+'__option';item.setAttribute('role','option');item.dataset.index=String(index);item.dataset.value=option.value;item.id=list.id+'-'+index;
        if(option.disabled)item.setAttribute('aria-disabled','true');
        const text=document.createElement('span');text.textContent=option.textContent;item.append(text);item.insertAdjacentHTML('beforeend',CHECK);
        return item;
      });
      list.replaceChildren(...menu.items);
      menu.panel.classList.toggle('is-searchable',options.length>7);
      if(!menu.panel.hidden)filter(menu);
    }
    menu.items.forEach((item,index)=>item.setAttribute('aria-selected',String(index===select.selectedIndex)));
  }
  function open(menu){
    if(!menu.panel.hidden||menu.trigger.getAttribute('aria-disabled')==='true')return;
    if(openMenu&&openMenu!==menu)close(openMenu);
    sync(menu);openMenu=menu;
    menu.search.value='';filter(menu);
    menu.panel.hidden=false;menu.trigger.setAttribute('aria-expanded','true');
    place(menu);
    activate(menu,menu.select.selectedIndex,true);
    if(menu.panel.classList.contains('is-searchable'))menu.search.focus();else menu.list.focus();
  }
  function close(menu,refocus){
    if(menu.panel.hidden)return;
    menu.panel.hidden=true;menu.trigger.setAttribute('aria-expanded','false');menu.trigger.removeAttribute('aria-activedescendant');menu.list.removeAttribute('aria-activedescendant');
    menu.active=-1;if(openMenu===menu)openMenu=null;
    if(refocus)menu.trigger.focus();
  }
  function place(menu){
    const {panel,trigger}=menu;panel.classList.remove('is-up','is-end');
    const anchor=trigger.getBoundingClientRect(),box=panel.getBoundingClientRect();
    if(anchor.bottom+box.height+12>window.innerHeight&&anchor.top-box.height-12>0)panel.classList.add('is-up');
    if(anchor.left+box.width>window.innerWidth-16&&anchor.right-box.width>=16)panel.classList.add('is-end');
  }
  function visible(menu){return menu.items.filter(item=>!item.hidden);}
  function filter(menu){
    const query=menu.search.value.trim().toLowerCase();
    let shown=0;
    menu.items.forEach(item=>{const match=!query||item.textContent.toLowerCase().includes(query);item.hidden=!match;if(match)shown++;});
    let empty=menu.list.querySelector('.'+CLASS+'__empty');
    if(!shown){if(!empty){empty=document.createElement('li');empty.className=CLASS+'__empty';empty.textContent='No matches';menu.list.append(empty);}}
    else if(empty)empty.remove();
    const first=visible(menu).findIndex(item=>item.getAttribute('aria-disabled')!=='true');
    activate(menu,first>=0?Number(visible(menu)[first].dataset.index):-1,false);
  }
  function activate(menu,index,scroll){
    menu.items.forEach(item=>item.classList.remove('is-active'));
    const item=menu.items[index];
    menu.active=item&&!item.hidden?index:-1;
    if(menu.active<0){menu.list.removeAttribute('aria-activedescendant');return;}
    item.classList.add('is-active');menu.list.setAttribute('aria-activedescendant',item.id);
    if(scroll!==false)item.scrollIntoView({block:'nearest'});
  }
  function move(menu,step){
    const shown=visible(menu).filter(item=>item.getAttribute('aria-disabled')!=='true');
    if(!shown.length)return;
    const position=shown.findIndex(item=>Number(item.dataset.index)===menu.active);
    let next;
    if(!Number.isFinite(step))next=step<0?0:shown.length-1;
    else if(position<0)next=step>0?0:shown.length-1;
    else next=Math.min(shown.length-1,Math.max(0,position+step));
    activate(menu,Number(shown[next].dataset.index),true);
  }
  function typeahead(menu,key){
    clearTimeout(menu.typeTimer);menu.query=(menu.query||'')+key.toLowerCase();menu.typeTimer=setTimeout(()=>{menu.query='';},600);
    const match=visible(menu).find(item=>item.getAttribute('aria-disabled')!=='true'&&item.textContent.toLowerCase().startsWith(menu.query));
    if(match)activate(menu,Number(match.dataset.index),true);
  }
  function choose(menu,index){
    const option=menu.select.options[index];
    if(!option||option.disabled)return;
    const changed=menu.select.selectedIndex!==index;
    menu.select.selectedIndex=index;
    close(menu,true);sync(menu);
    if(changed){menu.select.dispatchEvent(new Event('input',{bubbles:true}));menu.select.dispatchEvent(new Event('change',{bubbles:true}));}
  }
  document.addEventListener('pointerdown',event=>{if(openMenu&&!openMenu.wrap.contains(event.target))close(openMenu);});
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&openMenu)close(openMenu,true);});
  window.addEventListener('resize',()=>{if(openMenu)place(openMenu);});
  function scan(root){(root.querySelectorAll?Array.from(root.querySelectorAll('select')):[]).forEach(enhance);}
  function start(){
    scan(document);
    new MutationObserver(records=>{for(const record of records)for(const node of record.addedNodes){if(node.nodeType!==Node.ELEMENT_NODE)continue;if(node.matches('select'))enhance(node);else scan(node);}}).observe(document.body,{childList:true,subtree:true});
  }
  window.ForgeSelectMenu={enhance,scan};
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start);else start();
})();</script>'''
