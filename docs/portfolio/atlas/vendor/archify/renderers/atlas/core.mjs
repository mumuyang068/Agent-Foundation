// Shared by the offline viewer and Node tests. No DOM or project-specific data.
export const ATLAS_KINDS = ['architecture','workflow','sequence','dataflow','lifecycle','er','decision'];
export const ATLAS_RELATIONS = ['expands','example','related','continues'];
export function safeUrl(value) {
  if (typeof value !== 'string' || !value.trim() || /[\u0000-\u0020\u007f\\]/.test(value)) return false;
  const s=value.trim();
  if (s.startsWith('//')) return false;
  const scheme=/^([a-z][a-z0-9+.-]*):/i.exec(s);
  if (!scheme) return true;
  if (!['http','https','file'].includes(scheme[1].toLowerCase())) return false;
  try { const u=new URL(s); return u.protocol==='file:' ? s.startsWith('file:///') : Boolean(u.hostname); } catch { return false; }
}
export function escapeHtml(value) { return String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
export function bounded(value,min,max,fallback) {const n=Number(value);return Number.isFinite(n)?Math.min(max,Math.max(min,n)):fallback;}
export function parseRoute(hash, views, entry) {
  const [raw,query='']=String(hash||'').replace(/^#/,'').split('?');
  const view=views.find(v=>v.id===raw)||views.find(v=>v.id===entry)||views[0];
  const p=new URLSearchParams(query), nodes=view?.nodes||[];
  const focus=nodes.some(n=>n.id===p.get('focus'))?p.get('focus'):'';
  return {view:view?.id||'',focus,z:bounded(p.get('z')||1,.85,2,1),x:bounded(p.get('x')||0,0,1e7,0),y:bounded(p.get('y')||0,0,1e7,0),panel:Boolean(focus)&&p.get('panel')==='1'};
}
export function routeHash(state) {
  const p=new URLSearchParams();if(state.focus)p.set('focus',state.focus);
  if(state.panel)p.set('panel','1');
  if(state.z!==1)p.set('z',String(Math.round(state.z*1000)/1000));
  if(state.x)p.set('x',String(Math.round(state.x)));if(state.y)p.set('y',String(Math.round(state.y)));
  return '#'+state.view+(p.size?'?'+p.toString():'');
}
export function breadcrumb(view, views) {
  const result=[],seen=new Set();let current=view;
  while(current&&!seen.has(current.id)){result.unshift(current);seen.add(current.id);current=views.find(v=>v.id===current.parent);}
  return result;
}
export function followLink(link, views, remembered = {}) {
  const target=views.find(v=>v.id===link.target);if(!target)return null;
  return {...{view:target.id,focus:'',panel:false,z:1,x:0,y:0},...remembered,view:target.id,...(link.focus?{focus:link.focus,panel:true}:{})};
}
