// Project-owned viewer layout. The installed Archify renderer stays unchanged.
// The existing hidden/aria-expanded state remains the sidebar source of truth.
export function installAtlasLayout({document,window,requestAnimationFrame,matchMedia,ResizeObserver,MutationObserver}){
 const get=id=>document.getElementById(id),index=get('index'),list=get('view-list'),toggle=get('index-toggle'),backdrop=get('index-backdrop');
 const shell=document.querySelector('.shell'),header=document.querySelector('.app-header'),main=document.querySelector('main');
 const narrow=matchMedia('(max-width:760px)');let desktopHidden=narrow.matches?false:index.hidden,lastActive='';
 const schedule=fn=>requestAnimationFrame(fn);
 const measureHeader=()=>{const height=Math.ceil(header?.getBoundingClientRect?.().height||68);document.documentElement?.style?.setProperty('--atlas-header-height',height+'px');};
 const revealActive=(force=false)=>{
  if(index.hidden)return;
  const item=list.querySelector('[aria-current="page"]');if(!item){lastActive='';return;}
  const id=item.dataset.view;if(!force&&id===lastActive)return;lastActive=id;
  if(!item.getBoundingClientRect||!list.getBoundingClientRect)return;
  const rect=item.getBoundingClientRect(),bounds=list.getBoundingClientRect();
  if(rect.top<bounds.top)list.scrollTop+=rect.top-bounds.top;
  else if(rect.bottom>bounds.bottom)list.scrollTop+=rect.bottom-bounds.bottom;
 };
 const setOpen=(open,{focus=false,remember=true}={})=>{
  index.hidden=!open;shell.classList.toggle('index-hidden',!open);toggle.setAttribute('aria-expanded',String(open));
  const label=open?'收起目录':'展开目录';toggle.innerHTML='<span aria-hidden="true">'+(open?'‹':'›')+'</span> <span>'+label+'</span>';toggle.setAttribute('aria-label',label);toggle.title=label;
  backdrop.hidden=!(narrow.matches&&open);if(main)main.inert=narrow.matches&&open;
  if(!narrow.matches&&remember)desktopHidden=!open;
  if(open){schedule(()=>revealActive(true));if(focus)get('search').focus();}
  else if(focus||index.contains?.(document.activeElement))toggle.focus();
 };
 toggle.onclick=()=>setOpen(index.hidden,{focus:true});
 backdrop.onclick=()=>setOpen(false,{focus:true});
 index.addEventListener('click',event=>{if(narrow.matches&&event.target.closest('[data-view]'))setOpen(false,{remember:false});});
 document.addEventListener('keydown',event=>{
  if(event.key==='Escape'&&!index.hidden&&(narrow.matches||index.contains?.(event.target))){event.preventDefault();setOpen(false,{focus:true});}
 });
 const resizeMode=()=>{
  // Do not expose the desktop list over the narrow page automatically.
  setOpen(narrow.matches?false:!desktopHidden,{remember:false});measureHeader();
 };
 if(narrow.addEventListener)narrow.addEventListener('change',resizeMode);
 else if(narrow.addListener)narrow.addListener(resizeMode);
 window.addEventListener('resize',measureHeader);
 if(ResizeObserver)new ResizeObserver(measureHeader).observe(header);
 if(MutationObserver)new MutationObserver(()=>schedule(()=>revealActive())).observe(list,{childList:true});
 measureHeader();setOpen(!index.hidden,{remember:false});
 return {setOpen,revealActive,measureHeader};
}
export const atlasLayoutCSS=`
:root{--atlas-header-height:68px}
.app-header{position:sticky;top:0;z-index:60;min-height:68px}
.app-header strong{min-width:0}
#index-toggle{flex-shrink:0;min-height:44px}
.shell{align-items:start;min-height:calc(100vh - var(--atlas-header-height));min-height:calc(100dvh - var(--atlas-header-height))}
#index{position:sticky;top:var(--atlas-header-height);height:calc(100vh - var(--atlas-header-height));height:calc(100dvh - var(--atlas-header-height));min-height:0;display:flex;flex-direction:column;max-height:none}
.index-search{flex:0 0 auto;min-width:0}
#view-list{flex:1 1 auto;min-height:0;overflow:auto;overscroll-behavior:contain;scrollbar-gutter:stable;align-content:start;grid-auto-rows:max-content}
#view-list button{min-width:0;min-height:44px;white-space:normal;overflow-wrap:anywhere}
#index-backdrop{position:fixed;inset:var(--atlas-header-height) 0 0;z-index:40;border:0;border-radius:0;margin:0;padding:0;background:#11212e66;cursor:pointer;display:none}
@media(max-width:760px){
 .shell,.shell.index-hidden{display:block}
 #index{position:fixed;top:var(--atlas-header-height);bottom:0;left:0;z-index:50;width:min(320px,calc(100vw - 44px));height:auto;max-height:none;border-bottom:0;border-right:1px solid var(--line);box-shadow:var(--shadow);padding:20px 14px}
 #index-backdrop:not([hidden]){display:block}
}
`;
export function adaptAtlasLayout(html){
 if(html.includes('id="atlas-layout-style"'))throw Error('Atlas layout already applied; rebuild from the maintained model.');
 const searchStart='<nav id="index"',start=html.indexOf(searchStart),list=html.indexOf('<div id="view-list">',start);
 if(start<0||list<0)throw Error('Expected Archify index markup is missing');
 const open=html.indexOf('>',start)+1;
 html=html.slice(0,open)+'<div class="index-search">'+html.slice(open,list)+'</div>'+html.slice(list);
 html=html.replace('</head>',`<style id="atlas-layout-style">${atlasLayoutCSS}</style></head>`);
 const marker='<dialog id="prototype-dialog">';if(!html.includes(marker))throw Error('Expected prototype dialog is missing');
 html=html.replace(marker,'<button id="index-backdrop" type="button" hidden tabindex="-1" aria-label="关闭目录"></button>'+marker);
 const runtimeEnd='\nrender();\n\n})();</script>';
 if(!html.includes(runtimeEnd))throw Error('Expected Archify runtime boundary is missing');
 const install=`\n(${installAtlasLayout.toString()})({document,window,requestAnimationFrame,matchMedia,ResizeObserver:typeof ResizeObserver==='function'?ResizeObserver:null,MutationObserver:typeof MutationObserver==='function'?MutationObserver:null});`;
 return html.replace(runtimeEnd,'\nrender();'+install+'\n\n})();</script>');
}
