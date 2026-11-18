import fs from 'node:fs';
import {fileURLToPath} from 'node:url';
import {escapeHtml as esc} from './core.mjs';
const asset=name=>fs.readFileSync(fileURLToPath(new URL('../../assets/atlas/'+name,import.meta.url)),'utf8');
function wrap(text,width){
  const lines=[];let line='',used=0;
  for(const c of String(text||'')){const w=/[^\x00-\xff]/.test(c)?17:9;if(c==='\n'||used+w>width){lines.push(line);line='';used=0;if(c==='\n')continue;}line+=c;used+=w;}
  if(line)lines.push(line);return lines;
}
function textLines(text,x,y,width,cls='') {
  return `<text class="${cls}" x="${x}" y="${y}">${wrap(text,width).map((l,i)=>`<tspan x="${x}" dy="${i?24:0}">${esc(l)}</tspan>`).join('')}</text>`;
}
function nodeSvg(n,view){
  const {x,y,w,h}=n,shape=n.shape||(view.kind==='er'?'entity':view.kind==='lifecycle'?'state':'module');
  let body='';
  if(shape==='decision'){
    const lines=wrap(n.label,w*.58),baseline=y+h/2-(lines.length-1)*12;
    return `<g class="node" data-node="${n.id}" tabindex="0" role="button" aria-label="${esc(n.label)}"><title>${esc(n.rule||n.label)}</title><path class="node-shape decision" d="M${x+w/2} ${y} L${x+w} ${y+h/2} L${x+w/2} ${y+h} L${x} ${y+h/2}Z"/><text class="node-title" x="${x+w/2}" y="${baseline}" text-anchor="middle">${lines.map((line,i)=>`<tspan x="${x+w/2}" dy="${i?24:0}">${esc(line)}</tspan>`).join('')}</text></g>`;
  }
  if(shape==='document')body=`<path class="node-shape document" d="M${x} ${y}H${x+w-18}L${x+w} ${y+18}V${y+h}H${x}Z M${x+w-18} ${y}V${y+18}H${x+w}"/>`;
  else body=`<rect class="node-shape ${shape}" x="${x}" y="${y}" width="${w}" height="${h}" rx="${['state','initial','final'].includes(shape)?Math.min(h/2,32):6}"/>`;
  let cursor=y+28;
  const row=(value,cls)=>{if(!value)return '';const t=textLines(value,x+14,cursor,w-28,cls);cursor+=wrap(value,w-28).length*24;return t;};
  body+=row(n.actor,'actor');body+=row(n.label,'node-title');
  if(shape==='entity'){body+=`<path class="entity-rule" d="M${x} ${cursor-12}H${x+w}"/>`;cursor+=8;for(const f of n.fields||[])body+=row(f,'field');}
  else {body+=row(n.action,'action');body+=row(n.output?'→ '+n.output:'','output');}
  if(n.links?.length)body+=`<path class="drill-mark" d="M${x+w-24} ${y+h-16}h10m-4-4 4 4-4 4"/>`;
  return `<g class="node" data-node="${n.id}" tabindex="0" role="button" aria-label="${esc([n.actor,n.label,n.output].filter(Boolean).join('，'))}"><title>${esc(n.label)}</title>${body}</g>`;
}
function edgeSvg(e){
  const p=e.points,first=p[0],last=p.at(-1);
  const middle=Math.max(1,Math.floor(p.length/2)),a=p[middle-1],b=p[middle];
  const at=e.label_at||[(a[0]+b[0])/2,(a[1]+b[1])/2-10];
  const label=e.event ? `${e.event}${e.guard?' ['+e.guard+']':''}` : e.guard?`${e.label} [${e.guard}]`:e.label;
  const labelWidth=[...label].reduce((n,c)=>n+(/[^\x00-\xff]/.test(c)?15:8),0)+12;
  const arrow=e.kind==='association'?'':` marker-end="url(#atlas-arrow)"`;
  return `<g class="edge ${e.kind}"><polyline points="${p.map(x=>x.join(',')).join(' ')}"${arrow}/><rect class="edge-mask" x="${at[0]-labelWidth/2}" y="${at[1]-16}" width="${labelWidth}" height="22" rx="3"/><text class="edge-label" x="${at[0]}" y="${at[1]}" text-anchor="middle">${esc(label)}</text>${e.kind==='association'?`<text class="cardinality" x="${first[0]+10}" y="${first[1]-12}">${esc(e.from_cardinality)}</text><text class="cardinality" x="${last[0]-10}" y="${last[1]-12}" text-anchor="end">${esc(e.to_cardinality)}</text>`:''}</g>`;
}
export function renderViewSvg(v){
  let body='';
  for(const g of v.groups||[])body+=`<g class="group ${g.role}"><rect x="${g.x}" y="${g.y}" width="${g.w}" height="${g.h}" rx="8"/><text x="${g.x+16}" y="${g.y+26}">${esc(g.title)}</text></g>`;
  if(v.kind==='sequence'){
    for(const p of v.participants||[])body+=`<g class="participant"><rect x="${p.x-90}" y="32" width="180" height="60" rx="6"/>${textLines(p.label,p.x-75,60,150,'node-title')}<path class="lifeline" d="M${p.x} 92V${v.canvas.height-24}"/></g>`;
    for(const [i,m]of (v.messages||[]).entries()){
      const x1=v.participants.find(p=>p.id===m.from).x,x2=v.participants.find(p=>p.id===m.to).x;
      const line=x1===x2?`M${x1} ${m.y}h52v28h-52`:`M${x1} ${m.y}H${x2}`;
      body+=`<g class="message ${m.reply?'reply':''}"><path d="${line}" marker-end="url(#atlas-arrow)"/><text x="${Math.min(x1,x2)+18}" y="${m.y-12}">${i+1}. ${esc(m.label)}</text></g>`;
    }
  } else {for(const e of v.edges||[])body+=edgeSvg(e);for(const n of v.nodes||[])body+=nodeSvg(n,v);}
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${v.canvas.width} ${v.canvas.height}" width="${v.canvas.width}" height="${v.canvas.height}" role="group" aria-label="${esc(v.title)}"><defs><marker id="atlas-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 10 5 0 10Z"/></marker></defs>${body}</svg>`;
}
export function renderAtlas(s){
  const zh=s.meta.locale==='zh-CN';
  const tr=(a,b)=>zh?a:b;
  const core=fs.readFileSync(fileURLToPath(new URL('./core.mjs',import.meta.url)),'utf8').replace(/^export /gm,'');
  const data=JSON.stringify(s).replace(/</g,'\\u003c').replace(/\u2028/g,'\\u2028').replace(/\u2029/g,'\\u2029');
  return `<!doctype html><html lang="${s.meta.locale}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${esc(s.meta.title)}</title><style>${asset('style.css')}</style></head><body>
  <header class="app-header"><button id="index-toggle" aria-expanded="true" aria-controls="index">☰ <span>${tr('目录','Index')}</span></button><strong>${esc(s.meta.title)}</strong><button id="theme" aria-label="${tr('切换深浅主题','Switch color theme')}">◐</button></header>
  <div class="shell"><nav id="index" aria-label="${tr('图谱目录','Atlas index')}"><label for="search">${tr('查找问题或图','Find a question or view')}</label><input id="search" type="search" placeholder="${tr('输入关键词','Search')}"><div id="view-list"></div></nav>
  <main><div class="reading-bar"><button id="back">← ${tr('返回','Back')}</button><div id="breadcrumbs"></div></div><section class="heading"><p id="level"></p><h1 id="view-title"></h1><p id="question"></p><p id="meaning"></p></section>
  <div class="toolbar"><div id="siblings"></div><div class="canvas-tools"><button id="prototype">${tr('对应原型','Prototype')}</button><button id="zoom-out" aria-label="${tr('缩小','Zoom out')}">−</button><output id="zoom">100%</output><button id="zoom-in" aria-label="${tr('放大','Zoom in')}">＋</button><button id="fit">${tr('适合窗口','Fit')}</button><button id="export">SVG ↓</button></div></div>
  <div class="workspace"><section class="diagram-panel"><div id="canvas" tabindex="0" role="region" aria-label="${tr('可滚动图面；节点可用 Tab 选择','Scrollable diagram; Tab to select nodes')}"></div><div id="legend"></div></section><aside id="inspector" hidden aria-label="${tr('对象详情','Object details')}"></aside></div>
  <section id="connections" aria-label="${tr('继续深入','Explore further')}"></section><details id="references"><summary>${tr('本图依据与原记录','Sources and records')}</summary><div id="source-list"></div></details>
  </main></div><dialog id="prototype-dialog"><header><strong id="prototype-title"></strong><button id="prototype-close">← ${tr('返回图谱','Return to atlas')}</button></header><div id="prototype-content"></div></dialog><p id="announcement" class="sr-only" role="status" aria-live="polite"></p>
  ${s.views.map(v=>`<template id="diagram-${v.id}">${renderViewSvg(v)}</template>`).join('\n')}
  <script id="atlas-data" type="application/json">${data}</script><script>(()=>{'use strict';\n${core}\n${asset('runtime.js')}\n})();</script></body></html>`;
}
