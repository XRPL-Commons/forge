'use strict';
window.ForgeMarkdown={render(source,repo){
 const fragment=DOMPurify.sanitize(marked.parse(source,{gfm:true,breaks:false}),{
  ALLOWED_TAGS:['p','br','hr','h1','h2','h3','h4','h5','h6','strong','em','del','s','a','code','pre','blockquote','ul','ol','li','table','thead','tbody','tr','th','td','details','summary','input'],
  ALLOWED_ATTR:['href','title','start','align','type','checked','disabled','open'],
  ALLOW_DATA_ATTR:false,ALLOW_ARIA_ATTR:false,RETURN_DOM_FRAGMENT:true
 });
 for(const input of fragment.querySelectorAll('input')){if(input.type!=='checkbox')input.remove();else input.disabled=true}
 for(const link of fragment.querySelectorAll('a[href]')){
  // Issue-relative links belong to the repository, never to Forge's API routes.
  try{const url=new URL(link.getAttribute('href'),'https://github.com/'+repo+'/blob/HEAD/');
   if(!['https:','http:','mailto:'].includes(url.protocol))throw Error('Unsupported link');
   link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';
  }catch{link.removeAttribute('href')}
 }
 const wrapper=document.createElement('div');wrapper.append(fragment);return wrapper.innerHTML;
}};
