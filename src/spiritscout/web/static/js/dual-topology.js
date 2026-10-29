/* 双图独立视口，共用记录详情；两图之间只高亮显式关联。 */
const DualTopology = (() => {
  let session=null, views={}, selected=null, filters={assets:'',exploration:''}, stateFilter='';
  const width=248;
  const types={goal:'目标',hypothesis:'假设',attempt:'尝试',finding:'事实',evidence:'依据',rule:'规则',milestone:'历史推进',
    domain:'域名',ip:'主机',url:'网址',service:'服务',endpoint:'接口',app:'应用',asset:'资产'};
  const states={open:'待探索',running:'探索中',blocked:'受阻',supported:'有支持',refuted:'已反驳',exhausted:'已走不通',closed:'已结束'};
  const relations={evidence:'依据引用',derives:'派生',anchor:'关联资产',produces:'产出',progression:'推进顺序',
    contains:'包含',resolves_to:'解析到',exposes:'暴露',redirects_to:'跳转到',related:'关联'};
  const sideOf=id=>S.topoGraph?.assets.nodes.some(n=>n.id===id) ? 'assets' : 'exploration';
  function apply(pane) {
    const view=views[pane.dataset.graphPane];
    const stage=pane.querySelector('.dual-stage');
    if(view && stage) stage.style.transform=`translate(${view.x}px,${view.y}px) scale(${view.scale})`;
  }
  function fit(pane,readable=false) {
    const stage=pane.querySelector('.dual-stage');
    if(!stage) return;
    const w=Number(stage.dataset.width),h=Number(stage.dataset.height);
    const scale=Math.max(readable ? .75 : .04,Math.min((pane.clientWidth-32)/w,(pane.clientHeight-32)/h,1));
    views[pane.dataset.graphPane]={x:(pane.clientWidth-w*scale)/2,y:Math.max(16,(pane.clientHeight-h*scale)/2),scale};
    apply(pane);
  }
  function zoom(pane,factor,x=pane.clientWidth/2,y=pane.clientHeight/2) {
    const v=views[pane.dataset.graphPane];
    if(!v) return;
    const scale=Math.min(2.4,Math.max(.08,v.scale*factor));
    v.x=x-(x-v.x)*scale/v.scale;v.y=y-(y-v.y)*scale/v.scale;v.scale=scale;apply(pane);
  }
  function focus(id) {
    selected=id;
    const related=new Set(id ? [id] : []);
    const record=S.topoProcess?.get(id);
    const matches=e=>e.source===id || e.target===id || record?.relation && e.references?.some(r=>r.record_id===record.record_id);
    for(const e of S.topoGraph?.edges || []) if(matches(e)) {
      related.add(e.source);related.add(e.target);
    }
    document.querySelectorAll('#topo [data-node]').forEach(el=>{
      el.classList.toggle('is-selected',el.dataset.node===id);
      el.classList.toggle('is-muted',!!id && !related.has(el.dataset.node));
      el.classList.toggle('is-linked',!!id && el.dataset.node!==id && related.has(el.dataset.node));
    });
    document.querySelectorAll('#topo .fact-edge').forEach(el=>{
      el.classList.toggle('is-highlighted',el.dataset.source===id || el.dataset.target===id
        || !!record?.relation && related.has(el.dataset.source) && related.has(el.dataset.target));
    });
    const text=document.querySelector('#topo [data-dual-selection]');
    if(text) text.textContent=id ? `已选：${S.topoProcess.get(id)?.title || id} · ${related.size-1} 条关联记录` : '选择节点，联动查看两图中的关联记录';
  }
  function bind(pane) {
    let drag=null,down=null;
    pane.addEventListener('wheel',e=>{
      e.preventDefault();const rect=pane.getBoundingClientRect();
      zoom(pane,Math.exp(-e.deltaY*.0015),e.clientX-rect.left,e.clientY-rect.top);
    },{passive:false});
    pane.addEventListener('pointerdown',e=>{
      if(e.button!==0 || e.target.closest('button') || !views[pane.dataset.graphPane]) return;
      down={x:e.clientX,y:e.clientY,node:e.target.closest('[data-node]')?.dataset.node};
      drag={...views[pane.dataset.graphPane]};pane.setPointerCapture(e.pointerId);pane.classList.add('grabbing');
    });
    pane.addEventListener('pointermove',e=>{
      if(!drag) return;
      views[pane.dataset.graphPane]={...drag,x:drag.x+e.clientX-down.x,y:drag.y+e.clientY-down.y};apply(pane);
    });
    pane.addEventListener('pointerup',e=>{
      if(down?.node && Math.hypot(e.clientX-down.x,e.clientY-down.y)<6) showNodeDetail(down.node);
      down=null;drag=null;pane.classList.remove('grabbing');
    });
    pane.addEventListener('pointercancel',()=>{down=null;drag=null;pane.classList.remove('grabbing');});
    pane.addEventListener('dblclick',e=>{if(!e.target.closest('[data-node],button')) fit(pane);});
    pane.querySelectorAll('[data-node]').forEach(el=>el.onkeydown=e=>{
      if(e.key==='Enter' || e.key===' ') {e.preventDefault();showNodeDetail(el.dataset.node);}
    });
    pane.querySelectorAll('[data-zoom]').forEach(button=>button.onclick=()=>{
      if(button.dataset.zoom==='fit') fit(pane);else zoom(pane,Number(button.dataset.zoom));
    });
  }
  function paint(pane,graph) {
    const side=pane.dataset.graphPane,query=filters[side].toLocaleLowerCase();
    const nodes=graph.nodes.filter(n=>(!query || `${n.title} ${n.record_id || ''}`.toLocaleLowerCase().includes(query))
      && (side!=='exploration' || !stateFilter || n.state===stateFilter));
    const ids=nodes.map(n=>n.id),known=new Set(ids);
    const edges=graph.edges.filter(e=>known.has(e.source) && known.has(e.target));
    const tools=`<div class="dual-tools"><button data-zoom="1.25" aria-label="放大">＋</button><button data-zoom="0.8" aria-label="缩小">－</button><button data-zoom="fit" aria-label="适应画布">⤢</button></div>`;
    pane.innerHTML=tools+(nodes.length ? `<div class="dual-stage">${nodes.map(n=>{
      const links=S.topoGraph.links.filter(e=>e.source===n.id || e.target===n.id).length;
      const type=side==='assets' ? n.node_type || 'asset' : n.node_type || n.kind;
      const evidence={unverified:'依据待核实',observed:'已观察',supported:'有依据记录',independently_verified:'独立复测通过',conflicting:'存在冲突',expired:'已过期'}[n.evidence_state];
      return `<div class="tnode dual-node" data-node="${esc(n.id)}" data-kind="${esc(type)}" data-state="${esc(n.state || '')}" role="button" tabindex="0">
        <div class="dual-node-meta"><span>${esc(types[type] || type)}</span><span>${esc(states[n.state] || (n.kind==='milestone' ? 'Agent 说明' : evidence || ''))}</span></div>
        <b title="${esc(n.title)}">${esc(n.title)}</b>
        <small>${esc(n.reason || n.actor || '未记录来源')}</small>
        ${links ? `<span class="dual-anchor">${links} 条跨图关联 ↔</span>` : ''}</div>`;
    }).join('')}<svg class="edges"></svg></div>` : `<div class="dual-empty">${query || stateFilter ? '没有匹配的节点，清除筛选可查看全部。' : side==='assets' ? '尚未登记资产。Agent 发现的资产会在这里展开。' : '尚未记录探索链路。目标、假设、尝试及产出会在这里展开。'}</div>`);
    if(nodes.length) {
      const stage=pane.querySelector('.dual-stage');
      const elements=new Map([...pane.querySelectorAll('[data-node]')].map(el=>[el.dataset.node,el]));
      const heights=new Map(ids.map(id=>[id,elements.get(id).offsetHeight]));
      const preds=new Map(ids.map(id=>[id,edges.filter(e=>e.target===id).map(e=>e.source)]));
      const layout=TopologyLayout.board(ids,preds,heights,width,pane.clientWidth);
      for(const id of ids) elements.get(id).style.transform=`translate(${layout.x.get(id)}px,${layout.y.get(id)}px)`;
      stage.dataset.width=layout.width;stage.dataset.height=layout.height;
      stage.style.width=`${layout.width}px`;stage.style.height=`${layout.height}px`;
      const svg=stage.querySelector('svg');
      svg.setAttribute('width',layout.width);svg.setAttribute('height',layout.height);
      svg.innerHTML=`<defs><marker id="dual-arrow-${side}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 0L10 5L0 10Z" class="dual-arrow"/></marker></defs>`+edges.map(e=>{
        const down=layout.y.get(e.target)>layout.y.get(e.source),up=layout.y.get(e.target)<layout.y.get(e.source);
        let path;
        if(down || up) {
          const x1=layout.x.get(e.source)+width/2,x2=layout.x.get(e.target)+width/2;
          const y1=layout.y.get(e.source)+(down ? heights.get(e.source) : 0),y2=layout.y.get(e.target)+(down ? 0 : heights.get(e.target));
          const bend=Math.max(24,Math.abs(y2-y1)*.4)*(down ? 1 : -1);
          path=`M${x1},${y1} C${x1},${y1+bend} ${x2},${y2-bend} ${x2},${y2}`;
          const obstacles=nodes.filter(n=>n.id!==e.source && n.id!==e.target
            && layout.y.get(n.id)<Math.max(y1,y2) && layout.y.get(n.id)+heights.get(n.id)>Math.min(y1,y2)
            && Math.abs(layout.x.get(n.id)+width/2-x1)<width/2+12);
          if(obstacles.length) {
            const gutter=Math.max(...obstacles.map(n=>layout.x.get(n.id)+width))+18,step=down ? 18 : -18;
            path=`M${x1},${y1} C${x1},${y1+step} ${gutter},${y1+step} ${gutter},${y1+step*2} L${gutter},${y2-step*2} C${gutter},${y2-step} ${x2},${y2-step} ${x2},${y2}`;
          }
        } else {
          const forward=layout.x.get(e.target)>layout.x.get(e.source),right=forward ? width : 0;
          const x1=layout.x.get(e.source)+right,x2=layout.x.get(e.target)+width-right;
          const y1=layout.y.get(e.source)+heights.get(e.source)/2,y2=layout.y.get(e.target)+heights.get(e.target)/2;
          path=`M${x1},${y1} L${x2},${y2}`;
        }
        return `<path class="fact-edge ${e.kind==='progression' ? 'dual-sequence' : ''}" marker-end="url(#dual-arrow-${side})" data-source="${esc(e.source)}" data-target="${esc(e.target)}" d="${path}"><title>${esc(relations[e.kind] || '显式关联')}${e.kind==='progression' ? '（不代表证据因果）' : ''}</title></path>`;
      }).join('');
      if(!views[side]) fit(pane,true);else apply(pane);
    }
    bind(pane);focus(selected);
    const count=document.querySelector(`[data-dual-count="${side}"]`);
    if(count) count.textContent=`${nodes.length} / ${graph.nodes.length}`;
  }
  function render(state,milestones=[]) {
    if(session!==S.cur) {session=S.cur;views={};selected=null;filters={assets:'',exploration:''};stateFilter='';}
    const box=document.querySelector('#topo'),old=box.querySelector('#topo-detail');
    const active=document.activeElement,searchSide=active?.dataset.dualSearch;
    const ruleSearch=active?.hasAttribute('data-topo-search');
    const caret=searchSide || ruleSearch ? active.selectionStart : null;
    const activityOpen=!!box.querySelector('.dual-activity[open]');
    const detail=old && !old.hidden ? old._curId : null,scroll=old?.scrollTop || 0;
    const original=!!old?.querySelector('.topo-original[open]');
    const graph=TopologyLayout.dual(state.topology?.process || {},milestones);
    S.topoGraph=graph;S.topoProcess=new Map(graph.records.map(n=>[n.id,n]));
    box.classList.add('dual-topology');
    box.innerHTML=`<div class="dual-header"><div><b>资产与探索</b><span data-dual-selection></span></div><span>${graph.links.length} 条跨图关联</span></div>
      <div class="dual-grid">${['assets','exploration'].map(side=>`<section class="dual-panel"><header><div><b>${side==='assets' ? '资产图' : '探索链路'}</b><span data-dual-count="${side}"></span></div>
        <p>${side==='assets' ? '目标、入口与资产关系' : '目标 → 假设 → 尝试 → 有依据的产出'}</p>
        <div class="dual-filter"><input data-dual-search="${side}" type="search" aria-label="筛选${side==='assets' ? '资产' : '探索节点'}" placeholder="搜索${side==='assets' ? '地址、资产名称' : '探索节点、记录 ID'}" value="${esc(filters[side])}">
        ${side==='exploration' ? `<select data-dual-state aria-label="筛选探索状态"><option value="">全部状态</option>${Object.entries(states).map(([key,label])=>`<option value="${key}" ${stateFilter===key ? 'selected' : ''}>${label}</option>`).join('')}</select>` : ''}</div></header>
        <div class="dual-canvas" data-graph-pane="${side}"></div></section>`).join('')}</div>
      <details class="dual-activity"><summary>当前推进与执行状态${state.topology?.process?.has_more ? ' · 画布记录已截断，更早记录见黑板目录' : ''}</summary><section id="topo-live" class="topo-live"></section></details>
      <div class="topo-detail" id="topo-detail" hidden><div class="td-head"><b id="td-title"></b><button class="td-close" id="td-close" aria-label="关闭详情">×</button></div><div class="td-body" id="td-body"></div></div>`;
    for(const side of ['assets','exploration']) paint(box.querySelector(`[data-graph-pane="${side}"]`),graph[side]);
    box.querySelectorAll('[data-dual-search]').forEach(input=>input.oninput=()=>{
      const side=input.dataset.dualSearch;filters[side]=input.value;views[side]=null;
      paint(box.querySelector(`[data-graph-pane="${side}"]`),graph[side]);
    });
    box.querySelector('[data-dual-state]').onchange=e=>{
      stateFilter=e.target.value;views.exploration=null;paint(box.querySelector('[data-graph-pane="exploration"]'),graph.exploration);
    };
    box.querySelector('.dual-activity').open=activityOpen;
    if(detail && S.topoProcess.has(detail)) {
      showNodeDetail(detail);const restored=box.querySelector('#topo-detail');restored.scrollTop=scroll;
      if(original && restored.querySelector('.topo-original')) restored.querySelector('.topo-original').open=true;
    } else focus(null);
    const restored=searchSide ? box.querySelector(`[data-dual-search="${searchSide}"]`) : ruleSearch ? box.querySelector('[data-topo-search]') : null;
    if(restored) {restored.focus();if(caret!==null) restored.setSelectionRange(caret,caret);}
  }
  return {render,focus,sideOf};
})();
