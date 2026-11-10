/* 资产是独立资料图；会话画布只展示公开目标、意图与事实的探索关系。 */
const SessionTopology = (() => {
  const W=232;
  const kinds={origin:[I18n.t('起点'),'flag'],goal:[I18n.t('目标'),'target'],intent:[I18n.t('意图'),'compass'],attempt:[I18n.t('尝试'),'flask'],discovery:[I18n.t('新发现'),'bulb'],breakthrough:[I18n.t('重大突破'),'target'],turning_point:[I18n.t('方向转变'),'compass'],fact:[I18n.t('事实'),'flask'],vulnerability:[I18n.t('漏洞候选'),'bug'],hint:[I18n.t('提示'),'bulb']};
  const edgeMeta={tries:[I18n.t('验证方法'),'chain'],redirects:[I18n.t('转向'),'chain'],scope:[I18n.t('目标'),'spawn'],spawns:[I18n.t('派生'),'spawn'],stage:[I18n.t('展开'),'chain'],advance:[I18n.t('推进'),'chain'],derives:[I18n.t('意图链'),'chain'],produces:[I18n.t('产出'),'yield'],task_output:[I18n.t('任务产出'),'yield'],evidence:[I18n.t('依据'),'proof'],dependency:[I18n.t('目标依赖'),'chain'],hint:[I18n.t('提示'),'chain']};
  const statuses={open:I18n.t('待探索'),pending:I18n.t('待执行'),created:I18n.t('已创建'),queued:I18n.t('待执行'),running:I18n.t('执行中'),active:I18n.t('执行中'),waiting:I18n.t('等待中'),blocked:I18n.t('受阻'),supported:I18n.t('有支持'),refuted:I18n.t('已反驳'),exhausted:I18n.t('已走不通'),closed:I18n.t('已结束'),done:I18n.t('已完成'),completed:I18n.t('已完成'),failed:I18n.t('失败'),cancelled:I18n.t('已撤销'),pruned:I18n.t('已暂缓'),stopped:I18n.t('已停止'),interrupted:I18n.t('已中断'),candidate:I18n.t('候选'),accepted:I18n.t('已收录')};
  const evidence={unverified:I18n.t('待核实'),observed:I18n.t('已观察'),supported:I18n.t('有依据'),independently_verified:I18n.t('独立复测通过'),conflicting:I18n.t('有冲突'),expired:I18n.t('已过期')};
  const assetTypes={domain:I18n.t('域名'),ip:I18n.t('主机'),url:I18n.t('网址'),service:I18n.t('服务'),endpoint:I18n.t('接口'),app:I18n.t('应用'),script:I18n.t('脚本情报'),asset:I18n.t('资产')};
  const icons={flag:'<path d="M5 20V4m0 1c4-4 7 4 14 0v10c-7 4-10-4-14 0"/>',target:'<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="4"/><circle cx="12" cy="12" r="1"/>',compass:'<circle cx="12" cy="12" r="8"/><path d="m15 9-2 4-4 2 2-4 4-2Z"/>',flask:'<path d="M9 3h6m-5 0v7l-5 8c-1 2 0 3 2 3h10c2 0 3-1 2-3l-5-8V3M8 15h8"/>',bug:'<path d="M9 5 7 3m8 2 2-2M4 9h4m8 0h4M4 15h4m8 0h4M12 8v12"/><rect x="8" y="6" width="8" height="14" rx="4"/>',bulb:'<path d="M9 18h6m-5 3h4M8 14a6 6 0 1 1 8 0l-1 2H9l-1-2Z"/>'};
  const icon=name=>`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || icons.compass}</svg>`;
  let session=null,model=null,viewport=null,layout=null,visible=null,observer=null;
  const ui={query:'',state:'',collapsed:new Set(),manual:new Map(),assetQuery:'',assetPage:0,assetCollapsed:new Set()};
  function host() {return document.querySelector(S.tab==='assets' ? '#asset-library' : '#topo');}
  function detailPanel() {return host()?.querySelector('.topo-detail');}
  function detailMarkup(assets=false) {
    return I18n.html`<aside class="topo-detail rg-detail" id="${assets ? 'asset' : 'topo'}-detail" hidden aria-label="节点详情"><div class="td-head"><b class="td-title" id="${assets ? 'ad' : 'td'}-title"></b><button class="td-close" aria-label="关闭详情">×</button></div><div class="td-body"></div></aside>`;
  }
  function status(node) {
    if(['fact','vulnerability'].includes(node.view_kind))return evidence[node.evidence_state] || statuses[node.status] || I18n.t('待核实');
    if(node.view_kind==='hint')return I18n.t('参考线索');
    if(node.node_type==='milestone')return I18n.t('公开进展');
    return statuses[node.state || node.status] || I18n.t('待探索');
  }
  function relatedEdges() {return [...(model?.edges || []),...(model?.links || [])];}
  function focus(id) {
    const canvas=document.querySelector('#topo');if(!canvas || !model)return;
    const path=new Set(id ? [id] : []),queue=id ? [id] : [];
    for(let i=0;i<queue.length;i++)for(const edge of model.edges)if(edge.target===queue[i] && !path.has(edge.source)){path.add(edge.source);queue.push(edge.source);}
    for(const edge of relatedEdges())if(edge.source===id || edge.target===id){path.add(edge.source);path.add(edge.target);}
    canvas.querySelectorAll('[data-node]').forEach(el=>{
      el.classList.toggle('is-selected',el.dataset.node===id);
      el.classList.toggle('is-muted',!!id && !path.has(el.dataset.node));
    });
    canvas.querySelectorAll('.rg-edge').forEach(el=>el.classList.toggle('is-highlighted',!!id && path.has(el.dataset.source) && path.has(el.dataset.target)));
    document.querySelectorAll('#asset-library [data-topo-record]').forEach(el=>el.classList.toggle('is-selected',el.dataset.topoRecord===id));
  }
  function apply() {
    const stage=document.querySelector('#topo .rg-stage');
    if(stage && viewport)stage.style.transform=`translate(${viewport.x}px,${viewport.y}px) scale(${viewport.scale})`;
    miniViewport();
  }
  function bounds() {
    if(!layout || !visible?.nodes.length)return {left:0,top:0,width:320,height:240};
    const left=Math.min(0,...layout.x.values())-12,top=Math.min(0,...layout.y.values())-12;
    return {left,top,width:Math.max(layout.width,...visible.nodes.map(n=>layout.x.get(n.id)+W+24))-left,
      height:Math.max(layout.height,...visible.nodes.map(n=>layout.y.get(n.id)+layout.heights.get(n.id)+24))-top};
  }
  function fit(readable=false) {
    const canvas=document.querySelector('#topo .rg-canvas');if(!canvas?.clientWidth || !layout)return;
    const top=106,bottom=48;
    const area=bounds();
    let scale=Math.min((canvas.clientWidth-64)/area.width,(canvas.clientHeight-top-bottom)/area.height,1);
    scale=Math.max(readable ? .68 : .04,scale);
    viewport={scale,x:(canvas.clientWidth-area.width*scale)/2-area.left*scale,y:top-area.top*scale};
    if(readable && (area.width*scale>canvas.clientWidth-64 || area.height*scale>canvas.clientHeight-top-bottom)) {
      viewport.x=24-(layout.x.get('__process_origin') || 0)*scale;
      viewport.y=(canvas.clientHeight+top-bottom)/2-((layout.y.get('__process_origin') || 0)+(layout.heights.get('__process_origin') || 116)/2)*scale;
    }
    apply();
  }
  function zoom(factor,x,y) {
    const canvas=document.querySelector('#topo .rg-canvas');if(!viewport || !canvas)return;
    x ??= canvas.clientWidth/2;y ??= canvas.clientHeight/2;
    const scale=Math.max(.04,Math.min(2.6,viewport.scale*factor));
    viewport.x=x-(x-viewport.x)*scale/viewport.scale;viewport.y=y-(y-viewport.y)*scale/viewport.scale;viewport.scale=scale;apply();
  }
  function drawEdges() {
    const svg=document.querySelector('#topo .rg-edges');if(!svg || !layout || !visible)return;
    const byId=new Map(visible.nodes.map(n=>[n.id,n]));
    svg.innerHTML=`<defs>${['spawn','chain','yield','proof'].map(type=>`<marker id="rg-arrow-${type}" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto" viewBox="0 0 8 8"><path d="M0 0L8 4L0 8Z" class="rg-arrow ${type}"/></marker>`).join('')}</defs>`+visible.edges.map(edge=>{
      const meta=edgeMeta[edge.kind] || [I18n.t('关联'),'chain'];
      const x1=layout.x.get(edge.source)+W,x2=layout.x.get(edge.target),y1=layout.y.get(edge.source)+layout.heights.get(edge.source)/2,y2=layout.y.get(edge.target)+layout.heights.get(edge.target)/2;
      const back=x2<x1,bend=Math.max(36,Math.abs(x2-x1)*.48);
      const transition=edge.kind==='redirects' && layout.x.get(edge.source)===layout.x.get(edge.target);
      const path=transition ? `M${x1},${y1} C${x1+64},${y1} ${x1+64},${y2} ${x1},${y2}` : back ? `M${x1},${y1} C${x1+72},${y1-100} ${x2-72},${y2-100} ${x2},${y2}` : `M${x1},${y1} C${x1+bend},${y1} ${x2-bend},${y2} ${x2},${y2}`;
      return `<g class="rg-edge ${meta[1]} ${back || ['task_output','advance'].includes(edge.kind) ? 'is-context' : ''}" data-source="${esc(edge.source)}" data-target="${esc(edge.target)}"><path d="${path}" marker-end="url(#rg-arrow-${meta[1]})"/><text x="${transition ? x1+42 : (x1+x2)/2}" y="${(y1+y2)/2-7}" text-anchor="middle">${meta[0]}</text><title>${esc(edge.detail || `${byId.get(edge.source)?.title} → ${byId.get(edge.target)?.title}`)}</title></g>`;
    }).join('');
  }
  function miniViewport() {
    const rect=document.querySelector('#topo .rg-mini-window'),canvas=document.querySelector('#topo .rg-canvas');
    if(!rect || !viewport || !canvas || !layout)return;
    rect.setAttribute('x',-viewport.x/viewport.scale);rect.setAttribute('y',-viewport.y/viewport.scale);
    rect.setAttribute('width',canvas.clientWidth/viewport.scale);rect.setAttribute('height',canvas.clientHeight/viewport.scale);
    const percent=document.querySelector('#topo [data-rg-scale]');if(percent)percent.textContent=`${Math.round(viewport.scale*100)}%`;
  }
  function minimap() {
    const mini=document.querySelector('#topo .rg-minimap');if(!mini || !layout || !visible)return;
    const area=bounds();
    mini.innerHTML=I18n.html`<span>图形导航</span><svg viewBox="${area.left} ${area.top} ${area.width} ${area.height}" preserveAspectRatio="none">${visible.nodes.map(n=>`<rect x="${layout.x.get(n.id)}" y="${layout.y.get(n.id)}" width="${W}" height="${layout.heights.get(n.id)}" class="${n.view_kind}" rx="12"/>`).join('')}<rect class="rg-mini-window" fill="none" stroke-width="8"/></svg>`;
    miniViewport();
    mini.onclick=event=>{
      if(!event.detail){fit();return;}
      const bounds=mini.querySelector('svg').getBoundingClientRect(),canvas=document.querySelector('#topo .rg-canvas');
      viewport.x=canvas.clientWidth/2-(area.left+(event.clientX-bounds.left)/bounds.width*area.width)*viewport.scale;
      viewport.y=canvas.clientHeight/2-(area.top+(event.clientY-bounds.top)/bounds.height*area.height)*viewport.scale;apply();
    };
  }
  function bind(canvas) {
    let drag=null;
    canvas.onwheel=event=>{if(event.target.closest('.rg-legend,.rg-tools,.rg-minimap'))return;event.preventDefault();const r=canvas.getBoundingClientRect();zoom(Math.exp(-event.deltaY*.0014),event.clientX-r.left,event.clientY-r.top);};
    canvas.onpointerdown=event=>{
      if(event.button!==0 || event.target.closest('button,input,select,.rg-legend') || !viewport)return;
      const node=event.target.closest('[data-node]');
      drag={x:event.clientX,y:event.clientY,node:node?.dataset.node,view:{...viewport},nx:node ? layout.x.get(node.dataset.node) : 0,ny:node ? layout.y.get(node.dataset.node) : 0};
      canvas.setPointerCapture(event.pointerId);canvas.classList.add('is-dragging');
    };
    canvas.onpointermove=event=>{
      if(!drag)return;const dx=event.clientX-drag.x,dy=event.clientY-drag.y;
      if(drag.node) {
        const x=drag.nx+dx/viewport.scale,y=drag.ny+dy/viewport.scale;
        layout.x.set(drag.node,x);layout.y.set(drag.node,y);ui.manual.set(drag.node,{x,y});
        for(const el of canvas.querySelectorAll('[data-node]'))if(el.dataset.node===drag.node)el.style.transform=`translate(${x}px,${y}px)`;
        drawEdges();
      } else {viewport.x=drag.view.x+dx;viewport.y=drag.view.y+dy;apply();}
    };
    canvas.onpointerup=event=>{
      if(drag?.node && Math.hypot(event.clientX-drag.x,event.clientY-drag.y)<6) {ui.manual.delete(drag.node);showNodeDetail(drag.node);}
      if(drag?.node)minimap();drag=null;canvas.classList.remove('is-dragging');
    };
    canvas.onpointercancel=()=>{drag=null;canvas.classList.remove('is-dragging');};
    canvas.onkeydown=event=>{
      if(event.target!==canvas)return;
      if(event.key==='Home'){event.preventDefault();fit();}
      if(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key) && viewport){event.preventDefault();viewport.x+=event.key==='ArrowLeft' ? 60 : event.key==='ArrowRight' ? -60 : 0;viewport.y+=event.key==='ArrowUp' ? 60 : event.key==='ArrowDown' ? -60 : 0;apply();}
    };
    canvas.querySelectorAll('[data-node]').forEach(el=>el.onkeydown=event=>{if(event.target===el && ['Enter',' '].includes(event.key)){event.preventDefault();showNodeDetail(el.dataset.node);}});
    canvas.querySelectorAll('[data-collapse]').forEach(button=>button.onclick=()=>{const id=button.dataset.collapse;ui.collapsed.has(id) ? ui.collapsed.delete(id) : ui.collapsed.add(id);paint();});
  }
  function paint() {
    const canvas=document.querySelector('#topo .rg-canvas');if(!canvas?.clientWidth || !model)return;
    visible=TopologyLayout.reasoningVisible(model,ui.collapsed,ui.query,ui.state);
    const oldOrigin=layout?.x.has('__process_origin') ? {x:layout.x.get('__process_origin'),y:layout.y.get('__process_origin')} : null;
    canvas.innerHTML=I18n.html`<div class="rg-legend"><div>${Object.entries(kinds).filter(([key])=>!['breakthrough','turning_point'].includes(key)).map(([key,[label,glyph]])=>`<span><i class="rg-icon ${key}">${icon(glyph)}</i>${key==='discovery' ? I18n.t('关键进展') : label}</span>`).join('')}</div><div class="rg-edge-legend">${[['spawn',I18n.t('派生')],['chain',I18n.t('意图链')],['yield',I18n.t('产出')],['proof',I18n.t('依据')]].map(([key,label])=>`<span><i class="${key}"></i>${label}</span>`).join('')}</div></div>
      ${visible.nodes.length ? I18n.html`<div class="rg-stage">${visible.nodes.map(node=>{
        const [baseLabel,glyph]=kinds[node.view_kind] || kinds.intent,label=baseLabel,children=model.edges.filter(e=>e.source===node.id).length;
        const assets=model.links.filter(e=>e.target===node.id || e.source===node.id).length;
        const state=node.state || node.status || node.evidence_state || '';
        return `<article class="rg-node ${node.view_kind}" data-node="${esc(node.id)}" data-state="${esc(state)}" role="button" tabindex="0" aria-label="${esc(label+'：'+node.title)}"><header><span><i class="rg-icon ${node.view_kind}">${icon(glyph)}</i><b>${label}</b></span>${node.node_type==='hypothesis' ? I18n.html('<em>假设</em>') : ''}${children ? I18n.html`<button class="rg-collapse" data-collapse="${esc(node.id)}" aria-label="${ui.collapsed.has(node.id) ? I18n.t('展开') : I18n.t('收起')}${esc(node.title)}的分支" aria-expanded="${!ui.collapsed.has(node.id)}">${ui.collapsed.has(node.id) ? '+' : '−'}</button>` : ''}</header><p class="rg-node-title" title="${esc(node.title)}">${esc(node.title)}</p>${node.node_type==='milestone' ? `<p class="rg-basis" title="${esc(node.reason)}">${esc(node.reason)}</p>` : ''}<footer><span class="rg-status" data-status="${esc(state)}"><i></i>${esc(status(node))}</span><small>${node.view_kind==='attempt' || node.node_type==='milestone' ? I18n.t`${node.progress?.length || 0} 条执行记录` : assets ? I18n.t`${assets} 条资产关联` : node.record_id ? '#'+esc(node.record_id.slice(-6)) : node.view_kind==='origin' ? I18n.t('会话入口') : I18n.t('任务记录')}</small></footer></article>`;
      }).join('')}<svg class="rg-edges" aria-label="探索关系"></svg></div>` : I18n.html('<div class="rg-empty">没有匹配的节点，可清除筛选查看完整探索链路。</div>')}
      <div class="rg-tools"><button data-rg-zoom="1.2" aria-label="放大">＋</button><button data-rg-zoom="0.833333" aria-label="缩小">−</button><button data-rg-fit aria-label="适应画布">⤢</button><button data-rg-reset aria-label="重新布局">↻</button><span data-rg-scale></span></div>
      ${visible.nodes.length>1 ? I18n.html('<button class="rg-minimap" aria-label="图形导航，点击移动视野"></button>') : ''}`;
    const elements=new Map([...canvas.querySelectorAll('[data-node]')].map(el=>[el.dataset.node,el]));
    const heights=new Map(visible.nodes.map(n=>[n.id,elements.get(n.id).offsetHeight]));
    layout={...TopologyLayout.reasoningLayout(visible.nodes,visible.edges,heights,W),heights};
    for(const [id,position] of ui.manual)if(layout.x.has(id)){layout.x.set(id,position.x);layout.y.set(id,position.y);}
    for(const [id,el] of elements)el.style.transform=`translate(${layout.x.get(id)}px,${layout.y.get(id)}px)`;
    const stage=canvas.querySelector('.rg-stage');
    if(stage){stage.style.width=`${layout.width}px`;stage.style.height=`${layout.height}px`;}
    const svg=canvas.querySelector('.rg-edges');if(svg){svg.setAttribute('width',layout.width);svg.setAttribute('height',layout.height);}
    drawEdges();
    if(viewport && oldOrigin && layout.x.has('__process_origin')) {viewport.x+=(oldOrigin.x-layout.x.get('__process_origin'))*viewport.scale;viewport.y+=(oldOrigin.y-layout.y.get('__process_origin'))*viewport.scale;}
    minimap();if(!viewport)fit(true);else apply();bind(canvas);
    canvas.querySelectorAll('[data-rg-zoom]').forEach(button=>button.onclick=()=>zoom(Number(button.dataset.rgZoom)));
    canvas.querySelector('[data-rg-fit]').onclick=()=>fit();
    canvas.querySelector('[data-rg-reset]').onclick=()=>{ui.manual.clear();viewport=null;paint();};
    focus(detailPanel()?._curId || null);
  }
  function openAssets(id) {
    document.querySelector('[data-tab="assets"]')?.click();
    if(id && S.topoProcess?.has(id))showNodeDetail(id);
  }
  function wireRecords(panel) {
    panel.querySelectorAll('[data-topo-record]').forEach(button=>button.onclick=()=>showNodeDetail(button.dataset.topoRecord));
  }
  function assetRows() {
    const panel=document.querySelector('#asset-library'),list=panel?.querySelector('.rg-asset-results');if(!list)return;
    const groups=TopologyLayout.assetTree(model.assets,ui.assetQuery);
    ui.assetPage=Math.min(ui.assetPage,Math.max(0,Math.ceil(groups.length/40)-1));
    function branch(node) {
      const collapsed=!ui.assetQuery && ui.assetCollapsed.has(node.id),children=node.children.length;
      return `<li><div class="rg-asset-entry">${children ? `<button class="rg-asset-toggle" data-asset-collapse="${esc(node.id)}" aria-expanded="${!collapsed}" aria-label="${esc((collapsed ? I18n.t('展开') : I18n.t('收起'))+' '+node.title)}">${collapsed ? '▸' : '▾'}</button>` : '<span class="rg-asset-spacer"></span>'}${node.records.length ? `<button type="button" class="rg-asset-path" data-topo-record="${esc(node.records[0].id)}" title="${esc(node.id)}">${esc(node.title)}</button>` : `<span class="rg-asset-path" title="${esc(node.id)}">${esc(node.title)}</span>`}${node.records.map(n=>`<button class="rg-asset-record" data-topo-record="${esc(n.id)}"><span class="rg-asset-type">${esc(assetTypes[n.node_type] || I18n.t('资产'))}</span> <span>${esc(evidence[n.evidence_state] || I18n.t('待核实'))}</span></button>`).join('')}</div>${children && !collapsed ? `<ul>${node.children.map(branch).join('')}</ul>` : ''}</li>`;
    }
    list.innerHTML=groups.length ? `<ul class="rg-asset-tree">${groups.slice(ui.assetPage*40,ui.assetPage*40+40).map(branch).join('')}</ul>` : I18n.html('<div class="rg-asset-empty">尚无匹配的资产记录。资产发现和关系登记后会显示在这里。</div>');
    list.innerHTML+=I18n.html`<div class="rg-asset-pagination"><span>${model.assets.nodes.length} 条资产${model.has_more ? I18n.t(' · 当前为有界目录') : ''}</span><button data-asset-page="-1" ${!ui.assetPage ? 'disabled' : ''}>上一页</button><span>${ui.assetPage+1} / ${Math.max(1,Math.ceil(groups.length/40))}</span><button data-asset-page="1" ${(ui.assetPage+1)*40>=groups.length ? 'disabled' : ''}>下一页</button></div>`;
    wireRecords(list);list.querySelectorAll('[data-asset-page]').forEach(button=>button.onclick=()=>{ui.assetPage+=Number(button.dataset.assetPage);assetRows();});
    list.querySelectorAll('[data-asset-collapse]').forEach(button=>button.onclick=()=>{const id=button.dataset.assetCollapse;ui.assetCollapsed.has(id) ? ui.assetCollapsed.delete(id) : ui.assetCollapsed.add(id);assetRows();});
  }

  function render(state,progress=[],assets=false) {
    if(session!==S.cur){session=S.cur;viewport=null;layout=null;ui.query='';ui.state='';ui.collapsed.clear();ui.manual.clear();ui.assetQuery='';ui.assetPage=0;ui.assetCollapsed.clear();}
    model=TopologyLayout.reasoning(state,progress);S.topoGraph=model;S.topoProcess=new Map(model.records.map(n=>[n.id,n]));
    const panel=document.querySelector(assets ? '#asset-library' : '#topo');if(!panel)return;
    const same=panel.dataset.scrollSession===S.cur,previous=same?panel.querySelector('.topo-detail'):null,detailId=previous && !previous.hidden ? previous._curId : null,scroll=previous?.scrollTop || 0;
    const original=!!previous?.querySelector('.topo-original[open]'),activity=same?panel.querySelector('.rg-activity'):null;
    const activityPositions=activity?[activity,activity.querySelector('.topo-live')].filter(Boolean).map(el=>[el,el.scrollTop,el.scrollLeft]):[];
    const list=same?panel.querySelector('.rg-asset-results'):null,listTop=list?.scrollTop || 0,listLeft=list?.scrollLeft || 0,tableLeft=list?.querySelector('.rg-asset-table')?.scrollLeft || 0;
    const signature=JSON.stringify([S.cur,model.nodes.map(n=>[n.id,n.title,n.description,n.state,n.status,n.reason,n.next,n.evidence_refs,n.evidence_state,n.progress?.slice(-20)]),model.edges,model.assets,model.links,model.references,model.has_more,model.missing_intents,assets]);
    if(panel.dataset.graphSignature===signature && panel.children.length)return;
    const active=document.activeElement,search=panel.contains(active) && active.matches('input[type="search"]'),caret=search ? active.selectionStart : null;
    panel.dataset.graphSignature=signature;
    panel.dataset.scrollSession=S.cur;
    if(assets) {
      panel.className='asset-library';panel.innerHTML=I18n.html`<header class="rg-asset-head"><div><h3>资产与情报</h3><p>脚本、静态接口、source map 与其他入口统一保存；关系和探索假设通过引用关联。</p></div><label>查找资产<input type="search" data-asset-search placeholder="搜索域名、接口、脚本或资产类型" value="${esc(ui.assetQuery)}"></label></header><div class="rg-asset-results"></div>${detailMarkup(true)}`;
      assetRows();panel.querySelector('[data-asset-search]').oninput=event=>{ui.assetQuery=event.target.value;ui.assetPage=0;assetRows();};
      const restored=panel.querySelector('.rg-asset-results');restored.scrollTop=listTop;restored.scrollLeft=listLeft;
      const table=restored.querySelector('.rg-asset-table');if(table)table.scrollLeft=tableLeft;
    } else {
      const attempts=model.nodes.filter(n=>n.view_kind==='attempt').length;
      panel.className='card topo reasoning-topology';panel.innerHTML=I18n.html`<header class="rg-header"><div><b>探索链路</b><span>${model.nodes.filter(n=>n.view_kind==='goal').length} 个目标 · ${model.nodes.filter(n=>n.view_kind==='intent').length} 个意图 · ${attempts} 次尝试 · ${model.nodes.filter(n=>n.node_type==='milestone').length} 项关键进展 · ${model.nodes.filter(n=>['fact','vulnerability'].includes(n.view_kind)).length} 条事实</span></div><div class="rg-header-actions"><input data-rg-search type="search" aria-label="搜索探索节点" placeholder="搜索探索节点" value="${esc(ui.query)}"><select data-rg-state aria-label="筛选探索状态"><option value="">全部状态</option>${['open','running','blocked','done','refuted','exhausted'].map(key=>`<option value="${key}" ${ui.state===key ? 'selected' : ''}>${statuses[key]}</option>`).join('')}</select><button data-rg-references>资料与规则 ${model.references.length}</button><button data-rg-assets>资产 ${model.assets.nodes.length} ↗</button></div></header>${model.missing_intents ? I18n.html('<p class="rg-coverage" role="status">本会话未记录意图层级，当前展示目标与事实；公开进展和执行记录仍可展开查看。</p>') : ''}<div class="rg-canvas" tabindex="0" aria-label="探索链路画布，方向键平移，Home适应画布"></div><details class="rg-activity"><summary>执行记录与公开进展${model.has_more ? I18n.t(' · 部分历史记录未载入') : ''}</summary><section id="topo-live" class="topo-live"></section></details>${detailMarkup()}`;
      panel.querySelector('[data-rg-search]').oninput=event=>{ui.query=event.target.value;paint();};
      panel.querySelector('[data-rg-state]').onchange=event=>{ui.state=event.target.value;paint();};
      panel.querySelector('[data-rg-references]').onclick=()=>showNodeDetail('__process_references');
      panel.querySelector('[data-rg-assets]').onclick=()=>openAssets();
      if(activity){
        panel.querySelector('.rg-activity').replaceWith(activity);
        activityPositions.forEach(([el,top,left])=>{el.scrollTop=top;el.scrollLeft=left;});
      }
      paint();observer?.disconnect();if(typeof ResizeObserver!=='undefined'){observer=new ResizeObserver(()=>{if(S.tab==='topo' && panel.clientWidth){paint();}});observer.observe(panel.querySelector('.rg-canvas'));}
    }
    if(detailId && S.topoProcess.has(detailId)){showNodeDetail(detailId);const restored=detailPanel();if(original && restored.querySelector('.topo-original'))restored.querySelector('.topo-original').open=true;restored.scrollTop=scroll;}
    if(search){const input=panel.querySelector('input[type="search"]');input?.focus({preventScroll:true});if(input && caret!==null)input.setSelectionRange(caret,caret);}
  }
  function projectionDetail(record) {
    const related=model.edges.filter(e=>e.source===record.id || e.target===record.id);
    return I18n.html`<div class="td-agent">${esc(record.actor || (record.view_kind==='origin' ? I18n.t('会话任务') : I18n.t('已保存的任务规划')))}</div><p class="td-note topo-fact-text">${esc(record.title)}</p>${record.reason ? `<p class="td-note">${record.node_type==='milestone' ? I18n.html('<b>观察与判断依据</b><br>') : ''}${esc(record.reason)}</p>` : ''}${record.next ? I18n.html`<p class="td-note"><b>接下来验证</b><br>${esc(record.next)}</p>` : ''}${record.method_title && record.node_type==='milestone' ? I18n.html`<p class="td-note"><b>验证方法</b><br>${esc(record.method_title)}</p>` : ''}${record.evidence_refs?.length ? I18n.html`<div class="topo-related"><b>证据引用</b>${record.evidence_refs.map(ref=>{const raw=String(ref),version=raw.match(/@(\d+)$/),id=raw.replace(/@\d+$/,'');const target=model.records.find(r=>(r.id===id || r.id==='__process_bb:'+id) && (!version || String(r.version)===version[1]));return target ? `<button data-topo-record="${esc(target.id)}">${esc(target.title)}</button>` : `<p class="td-note">${esc(ref)}</p>`;}).join('')}</div>` : ''}${record.description ? `<p class="td-note">${esc(record.description)}</p>` : ''}<div class="topo-related"><b>探索关系</b>${related.map(e=>`<button data-topo-record="${esc(e.source===record.id ? e.target : e.source)}">${esc((edgeMeta[e.kind] || [I18n.t('关联')])[0])} · ${esc(S.topoProcess.get(e.source===record.id ? e.target : e.source)?.title || '')}</button>`).join('') || I18n.html('<p class="td-note">尚无明确关联记录。</p>')}</div>${record.progress?.length ? I18n.html`<details class="topo-original"><summary>本阶段的公开说明与执行 · ${record.progress.length} 条</summary>${record.progress.map(p=>`<div class="td-note">${p.body || p.title ? `<p>${esc(p.body || p.title)}</p>` : ''}${(p.tools || []).map(t=>`<p><b>${esc(t.name)}</b> · ${esc(statuses[t.status] || t.status)}${t.note ? ` · ${esc(t.note)}` : ''}</p>`).join('')}</div>`).join('')}</details>` : ''}`;
  }
  return {render,focus,detailPanel,projectionDetail,openAssets};
})();
