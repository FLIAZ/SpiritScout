/* 依照真实父边逐层向右展开；环边不参与布局，避免损坏的历史数据卡死画布。 */
const TopologyLayout = (() => {
  function layers(ids, preds) {
    const known = new Set(ids), depth = new Map(), visiting = new Set();
    const visit = id => {
      if (depth.has(id)) return depth.get(id);
      visiting.add(id);
      let d = 0;
      for (const parent of preds.get(id) || []) {
        if (known.has(parent) && !visiting.has(parent)) d = Math.max(d, visit(parent) + 1);
      }
      visiting.delete(id);
      depth.set(id, d);
      return d;
    };
    ids.forEach(visit);
    const columns = [];
    ids.forEach(id => (columns[depth.get(id)] ||= []).push(id));
    return {columns, depth};
  }

  function positions(columns, preds, heights, gap, pad) {
    const y = new Map();
    // 同一分支的后代对齐父节点；同列节点按实际高度避让。
    for (const column of columns) {
      let bottom = pad - gap;
      const wanted = id => {
        const parents = (preds.get(id) || []).filter(p => y.has(p));
        return parents.length ? parents.reduce((sum, p) => sum + y.get(p) + heights.get(p) / 2, 0)
          / parents.length - heights.get(id) / 2 : pad;
      };
      column.sort((a, b) => wanted(a) - wanted(b));
      for (const id of column) {
        const top = Math.max(pad, wanted(id), bottom + gap);
        y.set(id, top);
        bottom = top + heights.get(id);
      }
    }
    return {y, height: Math.max(pad, ...[...y].map(([id, top]) => top + heights.get(id))) + pad};
  }
  function project(process) {
    const records = (process.nodes || []).filter(n => ["finding", "evidence", "rule", "milestone"].includes(n.kind));
    const groups = new Map(), nodeToGroup = new Map(), facts = [];
    const labels = {assets:"资产与接口", evidence:"页面与响应依据", rules:"任务、授权与规则"};
    for (const node of records) {
      const type = node.reference_type || (node.layer === "asset" ? "assets" :
        node.kind === "evidence" || /^https?:\/\/\S+$/.test(node.title || "") ? "evidence" :
        node.kind === "rule" ? "rules" : "");
      if (!type) { facts.push({...node}); continue; }
      if (!groups.has(type)) groups.set(type, {id:`__process_group:${type}`, kind:"collection",
        title:labels[type] || "参考资料", members:[], reference_type:type});
      groups.get(type).members.push(node);
      nodeToGroup.set(node.id, groups.get(type).id);
    }
    const visible = [...groups.values(), ...facts], known = new Set(visible.map(n => n.id));
    const edges = new Map();
    for (const edge of process.edges || []) {
      const source = nodeToGroup.get(edge.source) || edge.source;
      const target = nodeToGroup.get(edge.target) || edge.target;
      if (!known.has(source) || !known.has(target) || source === target) continue;
      const key = JSON.stringify([source, target]);
      if (!edges.has(key)) edges.set(key, {source, target, references:[]});
      edges.get(key).references.push(edge);
    }
    const routes=new Map();
    for(const node of facts.filter(n=>n.kind==="milestone")) {
      const key=JSON.stringify([node.task_id,node.route_id]);
      if(!routes.has(key)) routes.set(key,[]);
      routes.get(key).push(node);
    }
    for(const route of routes.values()) {
      route.sort((a,b)=>a.turn-b.turn);
      route.forEach((node,i)=>{if(i) edges.set(JSON.stringify([route[i-1].id,node.id]),{
        source:route[i-1].id,target:node.id,kind:"progression",references:[]});});
    }
    const consumers = new Map();
    for (const edge of edges.values()) {
      if (!consumers.has(edge.source)) consumers.set(edge.source, new Set());
      consumers.get(edge.source).add(edge.target);
    }
    for (const fact of facts) fact.shared = (consumers.get(fact.id)?.size || 0) > 1;
    return {nodes:visible, records, groups:[...groups.values()], edges:[...edges.values()]};
  }

  function compact(ids, preds, heights, width, gapX, gapY, pad, references, routeNodes = []) {
    const referenceSet = new Set(references), milestones=new Set(routeNodes.map(n=>n.id));
    const factIds = ids.filter(id => !referenceSet.has(id) && !milestones.has(id));
    const factPreds = new Map(factIds.map(id => [id, (preds.get(id) || []).filter(p => !referenceSet.has(p))]));
    const shared = factIds.filter(id => (factIds.filter(n => (factPreds.get(n) || []).includes(id)).length > 1));
    const sharedSet = new Set(shared), main = factIds.filter(id => !sharedSet.has(id));
    // 共享事实从主区抽出只画一次；其引用仍保留为真实连线，不改变依赖深度。
    const graph = layers(factIds, factPreds), x = new Map(), y = new Map(), sections = [];
    let right = pad, bottom = pad;
    const place = (items, label, cols, snake=false) => {
      if (!items.length) return;
      const start = right;
      sections.push({label, x:start, y:pad, count:items.length});
      let top = pad + 36;
      for (let i = 0; i < items.length; i += cols) {
        const row = items.slice(i, i + cols);
        row.forEach((id, j) => { x.set(id, start + ((snake && Math.floor(i/cols)%2) ? cols-1-j : j) * (width + gapX)); y.set(id, top); });
        top += Math.max(...row.map(id => heights.get(id))) + gapY;
      }
      right += cols * (width + gapX) + gapX;
      bottom = Math.max(bottom, top);
    };
    place([...references, ...shared], "参考资料与共享事实", 1);
    const routes=new Map();
    for(const node of routeNodes) {
      const key=JSON.stringify([node.task_id,node.route_id]);
      if(!routes.has(key))routes.set(key,[]);
      routes.get(key).push(node);
    }
    for(const route of routes.values()) {
      route.sort((a,b)=>a.turn-b.turn);
      place(route.map(n=>n.id),`解题路线 · ${route[0].route_title || "调查"}`,Math.min(3,route.length),true);
    }
    for (const [depth, column] of graph.columns.entries()) {
      const items = column.filter(id => main.includes(id));
      place(items, depth ? `事实依赖 · 第 ${depth + 1} 层` : "观察与已知事实", Math.min(3, Math.ceil(Math.sqrt(items.length))));
    }
    return {x, y, width:Math.max(width + pad * 2, right), height:bottom + pad, sections};
  }
  function activity(state, summaries = []) {
    const byTask = new Map(summaries.map(row=>[row.task_id,row]));
    const tasks = state.tasks || [];
    const terminal = new Set(["done","failed","cancelled","pruned","completed","stopped","interrupted"]);
    const rows = tasks.map(task=>{
      const summary=byTask.get(task.task_id);
      const note=(task.routes || []).filter(route=>route.status==="running").map(route=>route.note).filter(Boolean).join("；")
        || task.result_summary || (task.routes || []).map(route=>route.note).filter(Boolean).at(-1);
      return {task_id:task.task_id, actor:summary?.actor || task.agent_type || task.task_type,
        title:summary?.title || task.title || "子任务", status:task.status,
        text:summary?.waiting || summary?.text || note || "尚未收到阶段说明。", updated:summary?.updated};
    });
    if (!tasks.length) rows.push(...summaries);
    return rows.sort((a,b)=>Number(terminal.has(a.status))-Number(terminal.has(b.status))
      || String(b.updated || "").localeCompare(String(a.updated || ""))).slice(0,4);
  }
  function dual(process, milestones = []) {
    const records=(process.nodes || []).map(n=>({...n}));
    // 资产关系以边展示，原记录留给详情追溯，避免再占探索节点。
    const nodes = records.filter(n=>!n.relation);
    const known = new Set(nodes.map(n=>n.id));
    const assetIds = new Set(nodes.filter(n=>{
      if(n.layer === 'asset' || n.reference_type === 'assets') return true;
      // 旧后端的 CTF 资产仍保留原始 type；无需改写历史黑板或按 URL 猜测。
      try {return n.layer==='topology' && n.kind==='finding' && JSON.parse(n.body || '{}').type==='asset';}
      catch {return false;}
    }).map(n=>n.id));
    const explicitTasks = new Set(nodes.filter(n=>n.kind==='exploration' && n.task_id).map(n=>n.task_id));
    // 历史会话仍可看公开推进阶段；有显式探索记录的任务不重复铺满逐轮日志。
    const fallback = milestones.filter(n=>!explicitTasks.has(n.task_id));
    nodes.push(...fallback.filter(n=>!known.has(n.id)));
    nodes.forEach(n=>known.add(n.id));
    const edgeMap=new Map();
    for(const e of process.edges || []) if(known.has(e.source) && known.has(e.target)) {
      const key=JSON.stringify([e.source,e.target,e.kind]);
      if(!edgeMap.has(key)) edgeMap.set(key,{...e,references:[]});
      edgeMap.get(key).references.push(e);
    }
    const edges=[...edgeMap.values()];
    const routes = new Map();
    for(const node of fallback) {
      const key=JSON.stringify([node.task_id,node.route_id]);
      if(!routes.has(key)) routes.set(key,[]);
      routes.get(key).push(node);
    }
    for(const route of routes.values()) {
      route.sort((a,b)=>a.turn-b.turn);
      route.slice(1).forEach((n,i)=>edges.push({source:route[i].id,target:n.id,kind:'progression',references:[]}));
    }
    const assets=nodes.filter(n=>assetIds.has(n.id)), exploration=nodes.filter(n=>!assetIds.has(n.id));
    const side=id=>assetIds.has(id) ? 'assets' : 'exploration';
    const pane=items=>({nodes:items,edges:edges.filter(e=>side(e.source)===side(e.target) && items.some(n=>n.id===e.source))});
    return {nodes,records:[...records,...fallback],groups:[],edges,assets:pane(assets),exploration:pane(exploration),
      links:edges.filter(e=>side(e.source)!==side(e.target))};
  }
  function board(ids,preds,heights,nodeWidth,viewportWidth) {
    const gap=32,pad=30,maxColumns=Math.max(1,Math.min(3,Math.floor((viewportWidth-pad*2+gap)/(nodeWidth+gap))));
    const columns=layers(ids,preds).columns;
    const used=Math.min(maxColumns,Math.max(1,...columns.map(c=>c.length)));
    const width=used*nodeWidth+(used-1)*gap+pad*2,x=new Map(),y=new Map();
    let top=pad;
    for(const column of columns) for(let i=0;i<column.length;i+=maxColumns) {
      const row=column.slice(i,i+maxColumns),left=(width-(row.length*nodeWidth+(row.length-1)*gap))/2;
      row.forEach((id,j)=>{x.set(id,left+j*(nodeWidth+gap));y.set(id,top);});
      top+=Math.max(...row.map(id=>heights.get(id)))+48;
    }
    return {x,y,width,height:top+pad-48};
  }
  return {layers, positions, project, compact, activity, dual, board};
})();
