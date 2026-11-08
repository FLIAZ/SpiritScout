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
    const labels = {assets:I18n.t("资产与接口"), evidence:I18n.t("页面与响应依据"), rules:I18n.t("任务、授权与规则")};
    for (const node of records) {
      const type = node.reference_type || (node.layer === "asset" ? "assets" :
        node.kind === "evidence" || /^https?:\/\/\S+$/.test(node.title || "") ? "evidence" :
        node.kind === "rule" ? "rules" : "");
      if (!type) { facts.push({...node}); continue; }
      if (!groups.has(type)) groups.set(type, {id:`__process_group:${type}`, kind:"collection",
        title:labels[type] || I18n.t("参考资料"), members:[], reference_type:type});
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
    place([...references, ...shared], I18n.t("参考资料与共享事实"), 1);
    const routes=new Map();
    for(const node of routeNodes) {
      const key=JSON.stringify([node.task_id,node.route_id]);
      if(!routes.has(key))routes.set(key,[]);
      routes.get(key).push(node);
    }
    for(const route of routes.values()) {
      route.sort((a,b)=>a.turn-b.turn);
      place(route.map(n=>n.id),I18n.t`解题路线 · ${route[0].route_title || I18n.t("调查")}`,Math.min(3,route.length),true);
    }
    for (const [depth, column] of graph.columns.entries()) {
      const items = column.filter(id => main.includes(id));
      place(items, depth ? I18n.t`事实依赖 · 第 ${depth + 1} 层` : I18n.t("观察与已知事实"), Math.min(3, Math.ceil(Math.sqrt(items.length))));
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
        title:summary?.title || task.title || I18n.t("子任务"), status:task.status,
        text:summary?.waiting || summary?.text || note || I18n.t("尚未收到阶段说明。"), updated:summary?.updated};
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
  function reasoning(state, milestones=[]) {
    const split=dual(state.topology?.process || {},[]), records=[...split.records], nodes=[], edges=[];
    const assets=split.assets.nodes, references=split.exploration.nodes.filter(n=>n.kind==='rule' || n.reference_type==='evidence');
    const excluded=new Set([...assets,...references].map(n=>n.id));
    const origin={id:'__process_origin',kind:'origin',view_kind:'origin',title:state.session?.title || state.session?.target || I18n.t('当前任务'),
      description:state.session?.target || I18n.t('会话任务入口'),status:state.session?.status,projection:true};
    nodes.push(origin);records.push(origin);
    const known=new Map(), goalByDag=new Map(), intentsByTask=new Map();
    const add=n=>{if(!known.has(n.id)){known.set(n.id,n);nodes.push(n);if(!records.some(r=>r.id===n.id))records.push(n);}return n.id;};
    const link=(source,target,kind,detail='')=>{
      if(source!==target && !edges.some(e=>e.source===source && e.target===target && e.kind===kind))
        edges.push({source,target,kind,detail,references:[]});
    };
    for(const record of split.exploration.nodes) {
      if(excluded.has(record.id)) continue;
      let payload={};try{payload=JSON.parse(record.body || '{}');}catch{/* 旧记录仍保留原文。 */}
      const view_kind=record.kind==='exploration' ? (record.node_type==='goal' ? 'goal' : record.node_type==='attempt' ? 'attempt' : 'intent') :
        record.layer==='vuln' ? 'vulnerability' : record.layer==='recon_clue' || payload?.type==='note' ? 'hint' : 'fact';
      add({...record,view_kind,task_id:record.task_id || record.provenance?.producer?.task_id || '',
        route_id:record.route_id || record.provenance?.producer?.route_id || ''});
      const taskId=known.get(record.id).task_id;
      if(record.kind==='exploration' && taskId && ['intent','attempt'].includes(view_kind)) {
        if(!intentsByTask.has(taskId))intentsByTask.set(taskId,[]);
        intentsByTask.get(taskId).push(record.id);
      }
    }
    const taskRows=new Map();
    for(const task of [...(state.topology?.tasks || []),...(state.tasks || [])])if(task.task_id) {
      const previous=taskRows.get(task.task_id);
      taskRows.set(task.task_id,{...previous,...task,delegation:task.delegation || previous?.delegation});
    }
    for(const dag of state.topology?.dag_nodes || []) {
      if(!dag.node_id) continue;
      const same=[...known.values()].find(n=>n.view_kind==='goal' && (dag.task_ids || []).includes(n.task_id));
      const id=same?.id || add({id:`__process_goal:${dag.node_id}`,kind:'goal',view_kind:'goal',title:dag.title || dag.tactic || I18n.t('任务目标'),
        status:dag.status,reason:dag.cancel_reason || dag.skip_reason || '',projection:true,dag_id:dag.node_id});
      goalByDag.set(dag.node_id,id);
      for(const taskId of dag.task_ids || []) if(!taskRows.has(taskId)) taskRows.set(taskId,{task_id:taskId,parent_dag_node:dag.node_id,status:dag.status});
    }
    for(const dag of state.topology?.dag_nodes || [])for(const parent of dag.depends_on || [])
      if(goalByDag.has(parent) && goalByDag.has(dag.node_id))link(goalByDag.get(parent),goalByDag.get(dag.node_id),'dependency',I18n.t('已保存的目标依赖'));
    for(const [taskId,task] of taskRows) {
      if(task.agent_type!=='scanner' || !task.delegation?.goal)continue;
      const parent=`__scan_assignment:${taskId}`;
      const id=add({id:`__process_scan_assignment:${taskId}`,kind:'goal',view_kind:'goal',
        title:task.delegation.goal,reason:task.delegation.reason || '',
        description:I18n.t`主 Agent 派遣探微 · ${task.delegation.target || ''}`,
        task_id:taskId,status:task.status,actor:'master',projection:true});
      goalByDag.set(parent,id);
      taskRows.set(taskId,{...task,parent_dag_node:parent});
      link(origin.id,id,'spawns',I18n.t('主 Agent 的真实扫描派遣，执行与其他角色并行'));
    }
    for(const fact of known.values())if(fact.task_id && !taskRows.has(fact.task_id))
      taskRows.set(fact.task_id,{task_id:fact.task_id,title:fact.actor ? I18n.t`${fact.actor} 的探索任务` : I18n.t('已记录的探索任务'),agent_type:fact.actor});
    const publicProgress=new Map(),stageRows=new Map();
    const stageKey=s=>JSON.stringify([s.task_id,s.route_id || '',s.turn>0 ? s.turn : s.step_id || s.id]);
    // 服务端目录独立于聊天窗口；实时正文只补充尚未进入目录的阶段。
    for(const stage of [...(state.topology?.public_stages?.items || []),...milestones]) {
      if(!stage.task_id)continue;
      const key=stageKey(stage);
      if(!stageRows.has(key))stageRows.set(key,stage);
    }
    for(const stage of stageRows.values()) {
      if(!publicProgress.has(stage.task_id))publicProgress.set(stage.task_id,[]);
      publicProgress.get(stage.task_id).push(stage);
    }
    for(const [taskId,stages] of publicProgress) if(!taskRows.has(taskId)) taskRows.set(taskId,{task_id:taskId,status:stages.at(-1)?.status});
    // 语义层级只来自模型明确的公开标注；工具回合仅作为尝试内部的详情。
    const routeStages=new Map(),attemptByScope=new Map();
    const semanticKey=(task,route,title)=>JSON.stringify([task,route || '',title]);
    for(const stage of stageRows.values()) {
      const scope=JSON.stringify([stage.task_id,stage.route_id || '']);
      if(!routeStages.has(scope))routeStages.set(scope,[]);
      routeStages.get(scope).push(stage);
    }
    for(const stages of routeStages.values()) {
      stages.sort((a,b)=>(a.turn || 0)-(b.turn || 0) || (a.sequence || 0)-(b.sequence || 0));
      let active=null;
      for(const stage of stages) {
        const annotation=stage.exploration;
        if(annotation && typeof annotation.intent==='string' && annotation.intent.trim()
            && typeof annotation.attempt==='string' && annotation.attempt.trim()) {
          const intentTitle=annotation.intent.trim(),attemptTitle=annotation.attempt.trim();
          const declaredIntent=nodes.filter(n=>n.view_kind==='intent' && n.task_id===stage.task_id && n.route_id===stage.route_id && n.title===intentTitle);
          const questionId=declaredIntent.length===1 ? declaredIntent[0].id : `__process_question:${semanticKey(stage.task_id,stage.route_id,intentTitle)}`;
          if(!known.has(questionId)) {
            add({id:questionId,kind:'intent',view_kind:'intent',node_type:'intent',title:intentTitle,
              task_id:stage.task_id,route_id:stage.route_id,actor:stage.actor,semantic_group:true,projection:true,
              status:taskRows.get(stage.task_id)?.status || 'running',sequence:stage.sequence || stage.turn || 0,progress:[]});
            if(!intentsByTask.has(stage.task_id))intentsByTask.set(stage.task_id,[]);
            intentsByTask.get(stage.task_id).push(questionId);
          }
          known.get(questionId).semantic_group=true;
          const key=JSON.stringify([stage.task_id,stage.route_id || '',intentTitle,attemptTitle]);
          const milestone=annotation.milestone;
          const validMilestone=milestone && ['discovery','breakthrough','turning_point'].includes(milestone.kind)
            && typeof milestone.title==='string' && milestone.title.trim()
            && typeof milestone.basis==='string' && milestone.basis.trim();
          const signature=validMilestone ? JSON.stringify([milestone.kind,milestone.title.trim(),milestone.basis.trim(),milestone.evidence_refs || []]) : '';
          const boundary=validMilestone && known.get(attemptByScope.get(key))?.milestone_signature!==signature;
          const previous=active;
          const declaredAttempt=nodes.filter(n=> {
            if(n.view_kind!=='attempt' || n.semantic_group || n.task_id!==stage.task_id || n.route_id!==stage.route_id || n.title!==attemptTitle)return false;
            const parents=split.edges.filter(e=>e.target===n.id && known.get(e.source)?.view_kind==='intent');
            return !parents.length || parents.some(e=>e.source===questionId);
          });
          // 方法名相同也可以有新的发现；普通回合继续归入该方法最近的里程碑。
          const id=boundary ? `__process_milestone:${stageKey(stage)}` : attemptByScope.get(key)
            || (declaredAttempt.length===1 ? declaredAttempt[0].id : null) || `__process_attempt:${key}`;
          if(!known.has(id))add({id,kind:boundary ? 'milestone' : 'intent',view_kind:boundary ? milestone.kind : 'attempt',
            node_type:boundary ? 'milestone' : 'attempt',title:boundary ? milestone.title.trim() : attemptTitle,
            method_title:attemptTitle,intent_title:intentTitle,task_id:stage.task_id,route_id:stage.route_id,
            actor:stage.actor,semantic_group:true,projection:true,sequence:stage.sequence || stage.turn || 0,
            progress:[],turns:[],tool_call_ids:[],
            ...(boundary ? {reason:milestone.basis,next:milestone.next || '',evidence_refs:milestone.evidence_refs || [],milestone_signature:signature,
              description:I18n.t('模型报告的公开发现；证据状态以原始记录和独立复核为准')} :
              {description:stage.semantic_origin==='reviewed_public_progress' ? I18n.t('依据已有公开说明整理') : I18n.t('模型公开声明的验证方法')})});
          if(active && active.id!==id && active.state==='running') {
            active.state='';
            if(!['failed','cancelled'].includes(active.status))active.status=active.progress.some(p=>p.pending_calls?.length) ? 'waiting' : 'done';
          }
          attemptByScope.set(key,id);active=known.get(id);
          active.semantic_group=true;active.progress ||= [];active.turns ||= [];active.tool_call_ids ||= [];
          active.method_title ||= attemptTitle;active.intent_title ||= intentTitle;
          if(active.node_type!=='milestone')active.reason=annotation.reason || active.reason || '';
          active.state=annotation.state || active.state || '';
          if(boundary && previous && previous.id!==id) {
            link(previous.id,id,'advance',I18n.t('同一路线的公开推进顺序；判断依据见节点，不表示独立验证通过'));
            if(previous.intent_title!==intentTitle)link(questionId,id,'tries',I18n.t('该发现所属的公开意图'));
          } else if(!edges.some(e=>e.target===id) && !split.edges.some(e=>e.source===questionId && e.target===id)) {
            link(questionId,id,'tries',I18n.t('该意图下模型明确声明的验证方法或发现'));
          }
          if(annotation.from && previous && previous.id!==id
              && [previous.title,previous.method_title].includes(annotation.from))
            link(previous.id,id,'redirects',annotation.reason || active.reason || I18n.t('模型明确标注的尝试转向'));
          else if(annotation.from)active.from_attempt=annotation.from;
        }
        if(!active)continue; // 无语义标注的历史保持公开记录，不能按时间臆造意图。
        active.progress.push(stage);
        active.turns.push(stage.turn);
        active.tool_call_ids.push(...(stage.tool_call_ids || []));
        active.status=stage.status || active.status;
        if(['done','completed','failed','cancelled','stopped'].includes(taskRows.get(stage.task_id)?.status)
            && active.state==='running')active.state='';
      }
    }
    for(const node of known.values())if(node.from_attempt) {
      const candidates=nodes.filter(n=>n.semantic_group && n.task_id===node.task_id && n.route_id===node.route_id && [n.title,n.method_title].includes(node.from_attempt) && n.id!==node.id);
      if(candidates.length===1)link(candidates[0].id,node.id,'redirects',node.reason || I18n.t('模型明确标注的尝试转向'));
    }
    // 公开事件用于分组和回放，不能覆盖 exploration_write 后来保存的当前修订。
    for(const record of split.exploration.nodes)if(record.kind==='exploration' && record.state) {
      const node=known.get(record.id);
      if(node) {node.state=record.state;node.reason=record.reason || '';}
    }
    for(const [taskId,task] of taskRows) {
      const parent=goalByDag.get(task.parent_dag_node);
      const explicit=intentsByTask.get(taskId) || [];
      const stages=publicProgress.get(taskId) || [];
      const latest=stages.slice().sort((a,b)=>(a.turn || 0)-(b.turn || 0)).at(-1);
      // 单个 DAG 目标已完整代表同名任务时，直接展开阶段，避免再复制一张同名意图卡。
      if(!explicit.length && stages.length && parent && task.title && task.title===known.get(parent)?.title
          && [...taskRows.values()].filter(t=>t.parent_dag_node===task.parent_dag_node).length===1) {
        intentsByTask.set(taskId,[parent]);known.get(parent).progress=stages;
      } else if(!explicit.length) {
        const id=add({id:`__process_intent:${taskId}`,kind:'intent',view_kind:'intent',task_id:taskId,
          title:task.title || latest?.route_title || latest?.title || task.agent_type || I18n.t('任务探索'),
          description:latest?.title || task.result_summary || '',status:task.status,actor:latest?.actor || task.agent_type,
          projection:true,progress:stages,body:task.result_summary || ''});
        intentsByTask.set(taskId,[id]);
      } else for(const id of explicit) {const n=known.get(id);if(!n.semantic_group && explicit.length===1)n.progress=stages;}
      for(const id of intentsByTask.get(taskId)) if(parent && !split.edges.some(e=>e.target===id && !excluded.has(e.source)))
        link(parent,id,'spawns',I18n.t('任务派发归属'));
    }
    for(const edge of split.edges) if(known.has(edge.source) && known.has(edge.target)) {
      let kind=edge.kind==='derives' && known.get(edge.source).view_kind==='goal' ? 'spawns' : edge.kind;
      if(kind==='derives') {
        const declared=edges.find(e=>e.source===edge.source && e.target===edge.target && ['redirects','tries'].includes(e.kind));
        if(declared)kind=declared.kind;
        else if(known.get(edge.source).view_kind==='intent' && known.get(edge.target).view_kind==='attempt')kind='tries';
      }
      link(edge.source,edge.target,kind,edge.detail);
      const saved=edges.find(e=>e.source===edge.source && e.target===edge.target && e.kind===kind);
      saved.references.push(...edge.references);
    }
    for(const fact of [...known.values()].filter(n=>['fact','vulnerability','hint'].includes(n.view_kind))) {
      if(!fact.task_id || edges.some(e=>e.target===fact.id)) continue;
      const source=fact.provenance?.producer || {};
      const exact=nodes.filter(n=>(n.view_kind==='attempt' || n.node_type==='milestone') && n.task_id===fact.task_id && n.route_id===fact.route_id
        && (source.tool_call_id && n.tool_call_ids?.includes(source.tool_call_id) || source.turn>0 && n.turns?.includes(source.turn)));
      if(exact.length===1){link(exact[0].id,fact.id,'task_output',I18n.t('产出来源明确记录了该任务、路线与执行步骤，不等于独立验证'));continue;}
      const intents=intentsByTask.get(fact.task_id) || [];
      // 只在归属唯一时展示任务产出；并行分支的歧义不能凭时间补边。
      const sameRoute=intents.filter(id=>fact.route_id && known.get(id).route_id===fact.route_id);
      const producer=sameRoute.length===1 ? sameRoute[0] : intents.length===1 ? intents[0] : null;
      if(producer) link(producer,fact.id,fact.view_kind==='hint' ? 'hint' : 'task_output',I18n.t('仅表示已记录的任务归属，不等于证据验证'));
    }
    for(const node of known.values()) if(['goal','intent','attempt'].includes(node.view_kind) && !edges.some(e=>e.target===node.id))
      link(origin.id,node.id,'scope',I18n.t('会话目标或探索归属，不等于证据因果'));
    const referenceGroup={id:'__process_references',kind:'collection',title:I18n.t('资料与规则'),members:references};
    records.push(referenceGroup);
    const enriched=new Map(nodes.map(n=>[n.id,n]));
    return {nodes,edges,records:records.map(n=>enriched.get(n.id) || n),groups:[referenceGroup],assets:split.assets,links:split.links,references,
      missing_intents:publicProgress.size>0 && !nodes.some(n=>n.semantic_group || n.kind==='exploration' && ['hypothesis','attempt'].includes(n.node_type)),
      has_more:!!(state.topology?.process?.has_more || state.topology?.public_stages?.has_more)};
  }
  function reasoningVisible(graph,collapsed=new Set(),query='',state='') {
    const hidden=new Set(),preds=new Map(graph.nodes.map(n=>[n.id,graph.edges.filter(e=>e.target===n.id).map(e=>e.source)]));
    const depth=layers(graph.nodes.map(n=>n.id),preds).depth;
    const forward=new Map([...preds].map(([id,parents])=>[id,parents.filter(parent=>depth.get(parent)<depth.get(id))]));
    let changed=true;
    while(changed) {
      changed=false;
      for(const node of graph.nodes) if(!hidden.has(node.id) && (forward.get(node.id) || []).length
        && forward.get(node.id).every(id=>hidden.has(id) || collapsed.has(id))) {hidden.add(node.id);changed=true;}
    }
    let visible=graph.nodes.filter(n=>!hidden.has(n.id));
    if(query || state) {
      const needle=query.trim().toLocaleLowerCase();
      const status=n=>({completed:'done',closed:'done',active:'running',pending:'open',created:'open',queued:'open'}[n.state || n.status] || n.state || n.status);
      const keep=new Set(visible.filter(n=>(!needle || `${n.title} ${n.reason || ''} ${n.next || ''} ${n.record_id || ''}`.toLocaleLowerCase().includes(needle))
        && (!state || status(n)===state)).map(n=>n.id));
      const queue=[...keep];
      for(let i=0;i<queue.length;i++)for(const parent of preds.get(queue[i]) || [])if(!keep.has(parent) && !hidden.has(parent)){keep.add(parent);queue.push(parent);}
      visible=visible.filter(n=>keep.has(n.id));
    }
    const ids=new Set(visible.map(n=>n.id));
    return {...graph,nodes:visible,edges:graph.edges.filter(e=>ids.has(e.source) && ids.has(e.target))};
  }
  function reasoningLayout(nodes,edges,heights,nodeWidth=232) {
    const ids=nodes.map(n=>n.id),preds=new Map(ids.map(id=>[id,edges.filter(e=>e.target===id && e.kind!=='redirects').map(e=>e.source)]));
    const base=layers(ids,preds),depth=new Map(),floor={origin:0,hint:0,goal:1,intent:2,attempt:3,discovery:3,breakthrough:3,turning_point:3,fact:4,vulnerability:4};
    for(const id of ids.slice().sort((a,b)=>base.depth.get(a)-base.depth.get(b))) {
      let d=floor[nodes.find(n=>n.id===id).view_kind] || 0;
      for(const parent of preds.get(id)) if(depth.has(parent) && base.depth.get(parent)<base.depth.get(id)) d=Math.max(d,depth.get(parent)+1);
      depth.set(id,d);
    }
    // 未标注意图、筛选或收起后可能缺层；稀疏数组的空位展开会让高度变成 NaN。
    const byDepth=[];for(const id of ids)(byDepth[depth.get(id)] ||= []).push(id);
    const columns=byDepth.filter(Boolean);
    const byId=new Map(nodes.map(n=>[n.id,n]));
    const order=(a,b)=>(byId.get(a).sequence ?? Number.MAX_SAFE_INTEGER)-(byId.get(b).sequence ?? Number.MAX_SAFE_INTEGER) || a.localeCompare(b);
    columns.forEach(column=>column.sort(order));
    const pad=48,gap=36,colGap=100,x=new Map(),y=new Map();
    const rowHeights=columns.map(c=>c ? c.reduce((s,id)=>s+(heights.get(id) || 120)+gap,0)-gap : 0);
    const height=Math.max(180,...rowHeights)+pad*2;
    const rank=new Map(columns.flat().map((id,i)=>[id,i]));
    for(let pass=0;pass<4;pass++) for(const column of columns.filter(Boolean)) {
      const center=id=>{const p=preds.get(id);return p.length ? p.reduce((s,k)=>s+(rank.get(k) || 0),0)/p.length : rank.get(id);};
      column.sort((a,b)=>center(a)-center(b) || order(a,b));column.forEach((id,i)=>rank.set(id,i));
    }
    columns.forEach((column,c)=>{
      if(!column)return;let top=pad+(height-pad*2-rowHeights[c])/2;
      for(const id of column){x.set(id,pad+c*(nodeWidth+colGap));y.set(id,top);top+=(heights.get(id) || 120)+gap;}
    });
    for(const column of columns.slice().reverse()) {
      let bottom=pad-gap;
      for(const id of column || []) {
        const align=['origin','goal','intent'].includes(byId.get(id).view_kind);
        const children=edges.filter(e=>align && e.source===id && e.kind!=='redirects' && y.has(e.target)).map(e=>e.target);
        const center=children.length ? children.reduce((sum,k)=>sum+y.get(k)+(heights.get(k) || 120)/2,0)/children.length : y.get(id)+(heights.get(id) || 120)/2;
        const top=Math.max(pad,bottom+gap,center-(heights.get(id) || 120)/2);
        y.set(id,top);bottom=top+(heights.get(id) || 120);
      }
    }
    return {x,y,width:pad*2+(columns.length || 1)*(nodeWidth+colGap)-colGap,
      height:Math.max(height,...ids.map(id=>y.get(id)+(heights.get(id) || 120)+pad)),depth};
  }
  return {layers, positions, project, compact, activity, dual, board,reasoning,reasoningVisible,reasoningLayout};
})();
