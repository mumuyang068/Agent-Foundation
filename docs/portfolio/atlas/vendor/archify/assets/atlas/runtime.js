const atlas=JSON.parse(document.getElementById('atlas-data').textContent);
const byId=id=>document.getElementById(id), zh=atlas.meta.locale==='zh-CN';
const t=(a,b)=>zh?a:b, h=escapeHtml;
const kindNames={architecture:t('架构','Architecture'),workflow:t('活动 / 泳道','Workflow'),sequence:t('时序','Sequence'),dataflow:t('数据流','Data flow'),lifecycle:t('状态','State'),er:t('实体关系','Entity relationships'),decision:t('条件分支','Decision')};
const levelNames={overview:t('总体位置','Overview'),mechanism:t('机制展开','Mechanism'),case:t('具体案例','Case')};
const relationNames={expands:t('展开机制','Expand'),example:t('看具体案例','Example'),related:t('相关问题','Related'),continues:t('继续这笔业务','Continue case')};
const edgeNames={handoff:t('交接','Handoff'),dependency:t('依赖','Dependency'),reference:t('引用','Reference'),data:t('数据传递','Data transfer'),transition:t('状态转移','Transition'),association:t('关联与基数','Association / cardinality')};
const meanings={architecture:t('范围框表示边界；位置按职责组织，连线按标注表达依赖、引用或交接。','Containers show boundaries; position groups responsibilities, edges carry their labeled semantics.'),workflow:t('泳道表示责任；沿箭头看动作、交出什么、由谁接收。','Lanes show responsibility; arrows show actions and handoffs.'),sequence:t('参与者从左到右排列；消息从上向下发生，虚线消息为返回。','Participants are arranged horizontally; time runs downward, dashed messages are replies.'),dataflow:t('沿箭头看资料如何转换、保存和被使用；存储位置与处理职责分别表达。','Follow data transformation, persistence and consumption; storage and processing remain distinct.'),lifecycle:t('圆角框表示状态；箭头标明触发事件与条件。','Rounded boxes are states; transitions show events and guards.'),er:t('属性列出主外键；连线表示关联，两端标注基数，不表示处理次序。','Attributes include keys; links show association and cardinality, not process order.'),decision:t('菱形表达判断；分支条件写在线上，结果各有去向。','Diamonds represent decisions; each branch states its condition and destination.')};
let state=parseRoute(location.hash,atlas.views,atlas.meta.entry), current, memory=new Map(), internalDepth=0, renderToken=0, dialogOrigin=null;
const canvas=byId('canvas'),inspector=byId('inspector'),dialog=byId('prototype-dialog');
function save(){state={...state,x:canvas.scrollLeft,y:canvas.scrollTop};memory.set(state.view,{...state});history.replaceState(null,'',routeHash(state));}
function viewLinks(v){return v.links||[];}
function jumpHtml(l){return `<button class="jump" data-jump="${h(l.target)}" data-focus="${h(l.focus||'')}"><strong>${h(l.label)}</strong><small>${h(l.reason)} · ${h(relationNames[l.relation]||'')}</small></button>`;}
function sourceHtml(id){const s=atlas.sources?.find(s=>s.id===id);return s?`<p><a href="${h(s.url)}" target="_blank" rel="noopener noreferrer">${h(s.title)} ↗</a><small> · ${h(s.kind==='record'?t('原记录','Record'):t('资料依据','Source'))}${s.version?' · '+h(s.version):''}</small>${s.note?`<small> · ${h(s.note)}</small>`:''}</p>`:'';}
function prototypeIds(){const n=current.nodes?.find(n=>n.id===state.focus);return [...new Set([...(n?.prototype_ids||[]),...(current.prototype_ids||[])])];}
function updateSelection(){
  const n=current.nodes?.find(n=>n.id===state.focus);
  canvas.querySelectorAll('[data-node]').forEach(el=>el.classList.toggle('selected',el.dataset.node===state.focus));
  inspector.hidden=!(state.panel&&n);byId('prototype').hidden=!prototypeIds().length;
  document.querySelector('.workspace').classList.toggle('with-detail',!inspector.hidden);
  if(inspector.hidden)return;
  const rows=[['actor',t('责任人 / 系统','Responsible role / system')],['input',t('接收','Receives')],['action',t('执行','Action')],['output',t('交出','Produces')],['next',t('接收后继续','What happens next')],['limits',t('条件与边界','Conditions and limits')]];
  inspector.innerHTML=`<button class="close" id="detail-close" aria-label="${t('关闭详情','Close details')}">×</button><h2>${h(n.label)}</h2>${rows.map(([k,label])=>{const value=n.detail?.[k]||n[k];return value?`<h3>${label}</h3><p>${h(value)}</p>`:'';}).join('')}${(n.links||[]).map(jumpHtml).join('')}${n.source_ids?.length?`<h3>${t('依据与记录','Sources and records')}</h3>${n.source_ids.map(sourceHtml).join('')}`:''}${n.prototype_ids?.length?`<button class="jump" data-prototype><strong>${t('查看对应原型','View corresponding prototype')}</strong><small>${t('在操作界面中查看这一步，返回后继续看图。','See this step in the UI, then return to the diagram.')}</small></button>`:''}`;
  byId('detail-close').onclick=()=>{state.panel=false;updateSelection();save();canvas.querySelector(`[data-node="${state.focus}"]`)?.focus();};
}
function renderIndex(){
  const query=byId('search').value.trim().toLowerCase();
  byId('view-list').innerHTML=atlas.views.filter(v=>`${v.title} ${v.question}`.toLowerCase().includes(query)).map(v=>`<button data-view="${v.id}" style="--depth:${breadcrumb(v,atlas.views).length-1}" class="${v.id===state.view?'active':''}" ${v.id===state.view?'aria-current="page"':''}>${h(v.title)}<small>${h(kindNames[v.kind])} · ${h(levelNames[v.level])}</small></button>`).join('');
}
function render(){
  clearTimeout(scrollTimer);
  const token=++renderToken;current=atlas.views.find(v=>v.id===state.view);
  byId('level').textContent=levelNames[current.level]+(current.case_id?' · '+current.case_id:'');
  byId('view-title').textContent=current.title;byId('question').textContent=current.question;
  byId('meaning').textContent=current.meaning||meanings[current.kind];
  byId('breadcrumbs').innerHTML=breadcrumb(current,atlas.views).map(v=>`<button data-view="${v.id}" ${v.id===state.view?'aria-current="page"':''}>${h(v.title)}</button>`).join('<span aria-hidden="true">›</span>');
  byId('back').disabled=!internalDepth&&!current.parent;
  const siblings=atlas.views.filter(v=>v.parent===current.parent&&v.level===current.level);
  byId('siblings').innerHTML=siblings.length>1?siblings.map(v=>`<button data-view="${v.id}" ${v.id===state.view?'aria-current="page"':''}>${h(kindNames[v.kind])}${siblings.filter(x=>x.kind===v.kind).length>1?' · '+h(v.title):''}</button>`).join(''):'';
  canvas.replaceChildren(byId('diagram-'+current.id).content.cloneNode(true));applyZoom();
  const kinds=[...new Set((current.edges||[]).map(e=>e.kind))];
  byId('legend').innerHTML=current.kind==='sequence'?h(t('实线消息：发起 / 调用；虚线消息：返回。','Solid: call / send. Dashed: reply.')):kinds.map(k=>`<span><i class="legend-line ${k}" aria-hidden="true"></i>${h(edgeNames[k])}</span>`).join('');
  byId('connections').innerHTML=viewLinks(current).map(jumpHtml).join('');
  const sources=[...new Set([...(current.source_ids||[]),...(current.nodes||[]).flatMap(n=>n.source_ids||[])])];
  byId('source-list').innerHTML=sources.length?sources.map(sourceHtml).join(''):`<p>${t('本图尚未绑定依据。','No sources have been bound to this view.')}</p>`;
  byId('references').open=false;renderIndex();updateSelection();
  requestAnimationFrame(()=>{if(token!==renderToken)return;canvas.scrollLeft=state.x;canvas.scrollTop=state.y;});
  byId('announcement').textContent=current.title+'。'+current.question;
  history.replaceState(null,'',routeHash(state));
}
function applyZoom(){const svg=canvas.querySelector('svg');if(!svg)return;svg.style.width=current.canvas.width*state.z+'px';svg.style.height=current.canvas.height*state.z+'px';byId('zoom').textContent=Math.round(state.z*100)+'%';}
function navigate(id,focus=''){
  save();const next=followLink({target:id,focus},atlas.views,memory.get(id));if(!next)return;
  state=next;internalDepth++;history.pushState(null,'',routeHash(state));render();byId('view-title').tabIndex=-1;byId('view-title').focus({preventScroll:true});
}
function select(id){state.focus=id;state.panel=true;updateSelection();save();}
document.addEventListener('click',event=>{
  const el=event.target.closest('[data-view],[data-jump],[data-node],[data-prototype]');if(!el)return;
  if(el.dataset.view)navigate(el.dataset.view);else if(el.dataset.jump)navigate(el.dataset.jump,el.dataset.focus);else if(el.dataset.node)select(el.dataset.node);else showPrototypes();
});
canvas.addEventListener('keydown',event=>{const n=event.target.closest('[data-node]');if(n&&(event.key==='Enter'||event.key===' ')){event.preventDefault();select(n.dataset.node);}if(event.key==='Escape'&&!inspector.hidden){state.panel=false;updateSelection();save();}});
let scrollTimer;canvas.addEventListener('scroll',()=>{clearTimeout(scrollTimer);scrollTimer=setTimeout(save,100);},{passive:true});
window.addEventListener('hashchange',()=>{state=parseRoute(location.hash,atlas.views,atlas.meta.entry);render();});
window.addEventListener('popstate',()=>{internalDepth=Math.max(0,internalDepth-1);state=parseRoute(location.hash,atlas.views,atlas.meta.entry);render();});
byId('back').onclick=()=>{if(internalDepth)history.back();else if(current.parent)navigate(current.parent);};
byId('search').oninput=renderIndex;
byId('index-toggle').onclick=()=>{const hidden=!byId('index').hidden;byId('index').hidden=hidden;document.querySelector('.shell').classList.toggle('index-hidden',hidden);byId('index-toggle').setAttribute('aria-expanded',String(!hidden));};
if(matchMedia('(max-width:760px)').matches)byId('index-toggle').click();
byId('theme').onclick=()=>document.body.classList.toggle('dark');
function zoomTo(z){save();const before=state.z;state.z=bounded(z,.85,2,1);applyZoom();canvas.scrollLeft=state.x/before*state.z;canvas.scrollTop=state.y/before*state.z;save();}
byId('zoom-in').onclick=()=>zoomTo(state.z+.1);byId('zoom-out').onclick=()=>zoomTo(state.z-.1);
byId('fit').onclick=()=>{state.z=Math.max(.85,Math.min(1.25,(canvas.clientWidth-20)/current.canvas.width,(canvas.clientHeight-20)/current.canvas.height));state.x=state.y=0;applyZoom();canvas.scrollTo(0,0);save();};
function showPrototypes(){
  save();dialogOrigin=document.activeElement;byId('prototype-title').textContent=t('对应原型','Corresponding prototype');
  byId('prototype-content').innerHTML=prototypeIds().map(id=>{const p=atlas.prototypes.find(p=>p.id===id);return `<section><h2>${h(p.title)}</h2><p>${h(p.purpose)}</p><p><a href="${h(p.url)}" target="_blank" rel="noopener noreferrer">${t('在新窗口打开','Open in a new window')} ↗</a></p>${p.embed?`<button class="jump" data-embed="${h(id)}">${t('在图谱内查看，关闭后回到当前位置','View here; closing returns to the current diagram')}</button>`:''}</section>`;}).join('');
  dialog.showModal();
}
byId('prototype').onclick=showPrototypes;
byId('prototype-content').onclick=event=>{const el=event.target.closest('[data-embed]');if(!el)return;const p=atlas.prototypes.find(p=>p.id===el.dataset.embed);if(!p||!p.embed)return;byId('prototype-title').textContent=p.title;const frame=document.createElement('iframe');frame.title=p.title;frame.setAttribute('sandbox','allow-scripts allow-forms');frame.src=p.url;byId('prototype-content').replaceChildren(frame);};
byId('prototype-close').onclick=()=>dialog.close();dialog.addEventListener('close',()=>{byId('prototype-content').replaceChildren();dialogOrigin?.focus();});
byId('export').onclick=()=>{const svg=canvas.querySelector('svg').cloneNode(true);svg.removeAttribute('style');const styles=document.createElementNS('http://www.w3.org/2000/svg','style');const vars=getComputedStyle(document.body);const values=['bg','surface','ink','muted','line','accent','soft','boundary','node','decision'].map(k=>'--'+k+':'+vars.getPropertyValue('--'+k)).join(';');styles.textContent=':root{'+values+'}\n'+document.querySelector('style').textContent;svg.prepend(styles);const url=URL.createObjectURL(new Blob([new XMLSerializer().serializeToString(svg)],{type:'image/svg+xml'}));const a=document.createElement('a');a.href=url;a.download=current.id+'.svg';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
render();
