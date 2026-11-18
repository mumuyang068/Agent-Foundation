import {ATLAS_KINDS, ATLAS_RELATIONS, safeUrl} from './core.mjs';
const idPattern=/^[a-z][a-z0-9_-]*$/i;
const nonempty=v=>typeof v==='string'&&v.trim().length>0;
const list=v=>Array.isArray(v)?v:[];
const numbers=v=>Array.isArray(v)&&v.length===2&&v.every(Number.isFinite);
export function validateAtlas(s) {
  const errors=[],warnings=[];
  const error=(code,at,message)=>errors.push({code,at,message});
  const required=(value,at)=>{if(!nonempty(value))error('required',at,'需要具体内容');};
  const ids=(items,at)=>{const set=new Set();for(const [i,item]of list(items).entries()){if(!item||!idPattern.test(item.id||''))error('id',`${at}[${i}]`,'ID 必须为字母开头的稳定标识');else if(set.has(item.id))error('duplicate',`${at}[${i}]`,'ID 重复');else set.add(item.id);}return set;};
  if(!s||typeof s!=='object'||Array.isArray(s))return {ok:false,errors:[{code:'schema',at:'$',message:'图谱必须为对象'}],warnings};
  if(s.schema_version!=='atlas-1')error('schema','schema_version','需要 atlas-1；单图规范不能直接拼成图谱');
  required(s.meta?.title,'meta.title');
  if(!['zh-CN','en'].includes(s.meta?.locale))error('locale','meta.locale','支持 zh-CN 或 en，正文由作者提供');
  if(!Array.isArray(s.views)||!s.views.length)error('views','views','至少提供一张解释具体问题的图');
  const viewIds=ids(s.views,'views'),sourceIds=ids(s.sources||[],'sources'),prototypeIds=ids(s.prototypes||[],'prototypes');
  if(!viewIds.has(s.meta?.entry))error('entry','meta.entry','入口视图不存在');
  for(const collection of ['sources','prototypes'])for(const [i,a]of list(s[collection]).entries()){
    const at=`${collection}[${i}]`;required(a?.title,at+'.title');if(!safeUrl(a?.url))error('url',at+'.url','需要安全的相对文件、HTTP(S) 或 file:/// 链接');
    if(collection==='sources'&&!['source','record'].includes(a?.kind))error('source-kind',at+'.kind','区分资料依据 source 和原记录 record');
    if(collection==='prototypes')required(a?.purpose,at+'.purpose');
  }
  const checkRefs=(owner,at)=>{
    for(const [field,set]of [['source_ids',sourceIds],['prototype_ids',prototypeIds]])for(const id of list(owner[field]))if(!set.has(id))error('reference',at+'.'+field,`未定义引用 ${id}`);
  };
  const checkLinks=(links,v,at)=>{for(const [i,l]of list(links).entries()){
    const a=`${at}[${i}]`,t=list(s.views).find(x=>x?.id===l?.target);
    if(!t){error('navigation',a,'目标视图不存在');continue;}
    required(l.label,a+'.label');required(l.reason,a+'.reason');
    if(!ATLAS_RELATIONS.includes(l.relation))error('relation',a+'.relation','需要 expands/example/related/continues 的明确关系');
    if(l.focus&&!list(t.nodes).some(n=>n?.id===l.focus))error('navigation',a+'.focus','目标节点不存在');
    if(l.relation==='expands'&&t.parent!==v.id)error('hierarchy',a,'展开目标必须声明当前视图为 parent');
    if(l.relation==='example'&&t.level!=='case')error('case',a,'案例链接必须进入独立 case 视图');
    if(l.relation==='continues'&&(!v.case_id||v.case_id!==t.case_id))error('case-join',a,'连续业务必须属于同一 case_id；独立研究只能 related');
  }};
  for(const [vi,v]of list(s.views).entries()){
    if(!v||typeof v!=='object')continue;
    const at=`views[${vi}]`;required(v.title,at+'.title');required(v.question,at+'.question');
    if(!ATLAS_KINDS.includes(v.kind))error('kind',at+'.kind','不支持的图型');
    if(!['overview','mechanism','case'].includes(v.level))error('level',at+'.level','需要 overview/mechanism/case');
    if(v.level==='case')required(v.case_id,at+'.case_id');
    if(v.parent&&!viewIds.has(v.parent))error('parent',at+'.parent','父视图不存在');
    const seen=new Set([v.id]);let parent=v.parent;
    while(parent){if(seen.has(parent)){error('cycle',at+'.parent','层次存在循环');break;}seen.add(parent);parent=list(s.views).find(x=>x?.id===parent)?.parent;}
    if(v.id!==s.meta?.entry&&!v.parent)warnings.push({code:'orphan',at,message:'无 parent，读者只能通过目录进入；确认是否为独立入口'});
    const width=v.canvas?.width,height=v.canvas?.height;
    if(!Number.isFinite(width)||!Number.isFinite(height)||width<300||height<200)error('canvas',at+'.canvas','提供可阅读的实际画布尺寸');
    checkRefs(v,at);checkLinks(v.links,v,at+'.links');
    const nodes=list(v.nodes),nodeIds=ids(nodes,at+'.nodes');ids(v.edges||[],at+'.edges');
    const groups=list(v.groups);const groupIds=ids(groups,at+'.groups');
    for(const [ni,n]of nodes.entries()){
      if(!n||typeof n!=='object')continue;
      const a=at+`.nodes[${ni}]`;required(n.label,a+'.label');
      if(n.shape&&!['module','document','decision','entity','state','initial','final'].includes(n.shape))error('shape',a+'.shape','节点形状不在支持的语义范围内');
      if(![n.x,n.y,n.w,n.h].every(Number.isFinite)||n.w<130||n.h<60||n.x<0||n.y<0||n.x+n.w>width||n.y+n.h>height)error('bounds',a,'节点需在画布内，且保留正常字号的空间');
      if(n.group&&!groupIds.has(n.group))error('group',a+'.group','所属边界/泳道不存在');
      if(['workflow','dataflow'].includes(v.kind)){required(n.actor,a+'.actor');required(n.output,a+'.output');}
      if(v.kind==='er'&&(!Array.isArray(n.fields)||!n.fields.length||!n.fields.every(nonempty)))error('entity',a+'.fields','实体须列出关键属性与主外键');
      if(v.kind==='lifecycle'&&!['state','initial','final'].includes(n.shape))error('state',a+'.shape','状态图节点须有状态语义');
      if(v.kind==='decision'&&n.shape==='decision')required(n.rule,a+'.rule');
      checkRefs(n,a);checkLinks(n.links,v,a+'.links');
      if(n.w&&n.h){
        const textRows=[...(n.actor?[n.actor]:[]),n.label,...(n.action?[n.action]:[]),...(n.output?['→ '+n.output]:[]),...list(n.fields)];
        let lineCount=0;for(const line of textRows)lineCount+=Math.max(1,Math.ceil([...String(line||'')].reduce((sum,c)=>sum+(/[^\x00-\xff]/.test(c)?17:9),0)/(n.w-28)));
        if(lineCount*24+24>n.h)error('text-fit',a,'文字超出节点空间；扩大节点或把补充解释移入详情，保留职责与输出');
      }
    }
    for(let i=0;i<nodes.length;i++)for(let j=i+1;j<nodes.length;j++){const a=nodes[i],b=nodes[j];if(a&&b&&a.x<b.x+b.w&&b.x<a.x+a.w&&a.y<b.y+b.h&&b.y<a.y+a.h)error('overlap',at+'.nodes',`${a.id} 与 ${b.id} 重叠`);}
    for(const [gi,g]of groups.entries()){
      const a=at+`.groups[${gi}]`;required(g?.title,a+'.title');
      if(!['boundary','lane'].includes(g?.role))error('group',a+'.role','边界或泳道需明确 role');
      if(!g||![g.x,g.y,g.w,g.h].every(Number.isFinite)||g.x<0||g.y<0||g.x+g.w>width||g.y+g.h>height)error('bounds',a,'边界/泳道超出画布');
      for(const n of nodes.filter(n=>n?.group===g?.id))if(n.x<g.x||n.y<g.y+32||n.x+n.w>g.x+g.w||n.y+n.h>g.y+g.h)error('containment',a,`${n.id} 没有处在声明的边界/泳道内`);
    }
    if(v.kind==='sequence'){
      if(nodes.length||list(v.edges).length)error('sequence',at,'时序图仅使用参与者和消息；普通节点/连线不会被渲染');
      const ps=list(v.participants),pids=ids(ps,at+'.participants');
      if(ps.length<2||!list(v.messages).length)error('sequence',at,'时序图需要参与者与消息，不能用普通方框冒充');
      for(const [pi,p]of ps.entries()){required(p?.label,at+`.participants[${pi}].label`);if(!Number.isFinite(p?.x)||p.x<100||p.x>width-100)error('bounds',at+'.participants','参与者横坐标需给名称留空间');}
      const ys=new Set();for(const [mi,m]of list(v.messages).entries()){
        const a=at+`.messages[${mi}]`;if(!pids.has(m?.from)||!pids.has(m?.to))error('message',a,'消息的参与者不存在');required(m?.label,a+'.label');
        if(!Number.isFinite(m?.y)||m.y<130||m.y>height-40||ys.has(m.y))error('message-order',a,'消息需要不同的纵向位置，保留时间次序');ys.add(m?.y);
      }
    }else if(!nodes.length)error('nodes',at+'.nodes','该图型需要节点');
    if(v.kind==='decision'&&!nodes.some(n=>n?.shape==='decision'))error('decision',at,'条件分支图需要明确的判断节点');
    for(const [ei,e]of list(v.edges).entries()){
      if(!e||typeof e!=='object')continue;
      const a=at+`.edges[${ei}]`;if(!nodeIds.has(e.from)||!nodeIds.has(e.to))error('edge',a,'连线端点不存在');required(e.label,a+'.label');
      if(!['handoff','dependency','reference','data','transition','association'].includes(e.kind))error('edge-kind',a+'.kind','连线语义须明确');
      if(v.kind==='er'&&(e.kind!=='association'||!nonempty(e.from_cardinality)||!nonempty(e.to_cardinality)))error('cardinality',a,'实体关系需两端基数；不要用处理箭头代替');
      if(v.kind==='lifecycle'&&(e.kind!=='transition'||!nonempty(e.event)))error('transition',a,'状态转移需触发事件及可选 guard');
      if(v.kind==='decision'&&!nonempty(e.guard))error('guard',a,'条件分支须说明判断条件');
      if(!Array.isArray(e.points)||e.points.length<2||!e.points.every(numbers)){error('route',a+'.points','连线需明确折点');continue;}
      for(const p of e.points)if(p[0]<0||p[1]<0||p[0]>width||p[1]>height)error('bounds',a+'.points','连线超出画布');
      for(let i=1;i<e.points.length;i++){
        const p=e.points[i-1],q=e.points[i];if(p[0]!==q[0]&&p[1]!==q[1])error('route',a+'.points','使用正交路径，保留明确进出方向');
        for(const n of nodes.filter(n=>n?.id!==e.from&&n?.id!==e.to))if((p[0]===q[0]&&p[0]>n.x&&p[0]<n.x+n.w&&Math.max(p[1],q[1])>n.y&&Math.min(p[1],q[1])<n.y+n.h)||(p[1]===q[1]&&p[1]>n.y&&p[1]<n.y+n.h&&Math.max(p[0],q[0])>n.x&&Math.min(p[0],q[0])<n.x+n.w))error('crossing',a,`连线穿过无关节点 ${n.id}`);
      }
      if(e.label_at&&!numbers(e.label_at))error('label',a+'.label_at','需要 [x,y]');
    }
  }
  return {ok:errors.length===0,errors,warnings};
}
