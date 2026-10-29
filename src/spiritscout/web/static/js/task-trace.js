"use strict";
/* 子任务保留身份；派发、推进阶段、结论按实际事件独立展示，共用历史与实时归属。 */
const TaskTrace = (() => {
  const terminal = new Set(["done", "failed", "cancelled", "pruned"]);
  const statuses = {running:"执行中", done:"已完成", failed:"执行失败", cancelled:"已撤销", pruned:"已暂缓", waiting:"等待返回"};
  const supported = /^(task\.(start|done|failed|cancelled|pruned)|agent\.(turn|delta|tool_call|tool_result|conclude|note|progress|ask_user|ask_answered|blocked|error)|model\.(started|call|failed|cancelled))$/;
  const wrapper = name => name === "investigate" || name === "explore";
  const worker = actor => !!actor && !/^(master(?:-|$)|gateway$|runtime$|system$|user$)/.test(actor);
  const create = () => ({tasks:new Map(), routes:new Map(), calls:new Map()});
  const keyOf = (actor, id) => JSON.stringify([actor, id]);
  const brief = text => String(text || "").replace(/\s+/g, " ").trim().slice(0, 180);
  const textOf = value => typeof value === "string" ? value : value == null ? "" : JSON.stringify(value, null, 2);
  function resolve(store, e) {
    const d = e.detail || {}, actor = String(e.actor || "");
    if (d.task_id) return keyOf(actor, d.task_id);
    if (d.call_id && store.calls.has(keyOf(actor,d.call_id))) return store.calls.get(keyOf(actor,d.call_id));
    for (const route of [d.route_id, d.route].filter(Boolean)) {
      const matches = store.routes.get(keyOf(actor,route));
      if (matches?.size === 1) return [...matches][0];
      if (matches?.size > 1) return keyOf(actor, "");
    }
    const candidates = [...store.tasks].filter(([, t]) => t.actor === actor && !terminal.has(t.status));
    return candidates.length === 1 ? candidates[0][0] : keyOf(actor, "");
  }
  function reduce(store, e) {
    if (!worker(e.actor) || !supported.test(e.type)) return null;
    const d = e.detail || {}, key = resolve(store,e);
    let task = store.tasks.get(key);
    if (!task) {
      task = {key, id:d.task_id || "", actor:e.actor, title:"", status:"running", progress:"正在准备", tools:new Map(),
        texts:new Map(), steps:new Map(), activeSteps:new Map(), routeAliases:new Map(),
        conclusions:new Map(), modelSteps:new Map(), started:false, summary:"", updated:e.ts, event:e, version:0};
      store.tasks.set(key,task);
    }
    task.updated=e.ts; task.version++;
    if (d.title) task.title=d.title;
    if (!task.title && d.angle) task.title=d.angle;
    for (const route of [d.route_id,d.route].filter(Boolean)) {
      const routeKey=keyOf(e.actor,route);
      if (!store.routes.has(routeKey)) store.routes.set(routeKey,new Set());
      store.routes.get(routeKey).add(key);
    }
    if(d.call_id) store.calls.set(keyOf(e.actor,d.call_id),key);
    if(d.route_id && d.route) {
      if(!task.routeAliases.has(d.route)) task.routeAliases.set(d.route,new Set());
      task.routeAliases.get(d.route).add(d.route_id);
    }
    const aliases=task.routeAliases.get(d.route);
    let route=d.route_id || (aliases?.size === 1 ? [...aliases][0] : d.route) || "";
    let step=route ? task.activeSteps.get(route) : null;
    const ensureStep = (stepKey, turn=0) => {
      if(!task.steps.has(stepKey)) {
        task.steps.set(stepKey,{key:stepKey,route,turn,event:e,texts:new Map(),status:terminal.has(task.status) ? task.status : "running",progress:"正在分析"});
      }
      const next=task.steps.get(stepKey), previous=task.activeSteps.get(route);
      // 迟到事件只更新原阶段；不会把当前阶段回退，也不提前宣告在途工具完成。
      if(!previous || !turn || turn>=previous.turn) {
        if(previous && previous!==next && !terminal.has(previous.status)) {
          previous.status=[...task.tools.values()].some(t=>t.stepKey===previous.key && t.status==="running") ? "waiting" : "done";
        }
        task.activeSteps.set(route,next);
      }
      return next;
    };
    if(route && Number.isInteger(d.turn) && d.turn>0) step=ensureStep(JSON.stringify([route,d.turn]),d.turn);
    else if(d.call_id && task.modelSteps.has(d.call_id)) step=task.steps.get(task.modelSteps.get(d.call_id));
    // 没有回合号的旧记录与 SRC 外层动作，按实际调用建立阶段，不凭空生成计划步骤。
    if(e.type === "agent.tool_call" && (d.calls || []).some(c=>!wrapper(c.name)) && (!step || !step.turn && terminal.has(step.status))) {
      step=ensureStep(JSON.stringify([route,"tool",d.calls[0]?.id || e.seq || e.ts]));
    }
    if(e.type === "model.started" && route && (!step || !step.turn && terminal.has(step.status))) {
      step=ensureStep(JSON.stringify([route,"model",d.call_id || e.seq || e.ts]));
    }
    if(d.call_id && step) task.modelSteps.set(d.call_id,step.key);
    if(step && !route) route=step.route;
    const stem=JSON.stringify([route,d.call_id || d.turn || ""]);
    if (e.type === "task.start") { task.started=true;task.status="running";task.progress="开始执行";task.target=d.target || ""; }
    if (e.type === "agent.turn") {
      task.progress="正在推进";
      if(step) {step.title=d.angle || step.title;step.progress="正在分析";}
    }
    if (e.type === "model.started") {task.progress="正在分析下一步";if(step) {step.progress="正在分析";if(!terminal.has(task.status)) step.status="running";}}
    if (e.type === "agent.delta") {
      if (d.kind === "reset") {task.texts.delete(stem);step?.texts.delete(stem);}
      else if (d.kind !== "reasoning" && d.kind !== "tool_call" && d.text) {
        const text=(task.texts.get(stem) || "")+d.text;
        task.texts.set(stem,text.slice(-24000)); task.progress="正在更新进展";
        if(step) {step.texts.set(stem,text.slice(-24000));step.progress="正在更新进展";}
      }
    }
    if (e.type === "agent.progress") {
      const text=commentaryText(d.text);
      if(text) {
        const publicKey=`progress:${d.call_id || e.seq || e.ts || task.version}`;
        task.texts.set(publicKey,text);
        if(step) step.texts.set(publicKey,text);
      }
    }
    if (e.type === "agent.ask_user" || e.type === "agent.blocked") {
      task.waitingReasons ||= new Map();
      task.waitingReasons.set(route,textOf(d.question || d.reason || d.message || "等待补齐必要条件"));
      task.waitingReason=[...task.waitingReasons.values()].join("；");
      if(step && !terminal.has(task.status)) {step.status="waiting";step.progress=task.waitingReason;}
    }
    if (e.type === "agent.ask_answered") {
      task.waitingReasons?.delete(route);
      task.waitingReason=[...(task.waitingReasons?.values() || [])].join("；");
      if(step && !terminal.has(task.status)) {step.status="running";step.progress="已收到答复，正在继续验证";}
    }
    if (e.type === "agent.note") {
      task.progress="已更新进展";task.texts.set(`note:${e.seq || e.ts}`,textOf(d.text));
      if(step) step.texts.set(`note:${e.seq || e.ts}`,textOf(d.text));
    }
    if (e.type === "agent.tool_call") {
      for (const c of d.calls || []) {
        const toolKey=JSON.stringify([route,c.id || `${e.seq || e.ts}:${task.tools.size}`]);
        task.tools.set(toolKey,{key:toolKey,call:c,route,id:c.id,name:c.name,status:"running",result:"",note:"",version:1,
          stepKey:wrapper(c.name) ? "" : step?.key || ""});
      }
      if(step) {step.status=terminal.has(task.status) ? task.status : "running";step.progress="工具执行中";}
      task.progress=(d.calls || []).length ? `正在调用 ${d.calls.map(c => SessionObserver.toolName(c.name)).join("、")}` : task.progress;
    }
    if (e.type === "agent.tool_result") {
      let tool=task.tools.get(JSON.stringify([route,d.id]));
      if (!tool && d.id) {
        const matches=[...task.tools.values()].filter(t => t.id === d.id);
        if(matches.length === 1) tool=matches[0];
      }
      if (!tool) {
        const toolKey=JSON.stringify([route,d.id || `result:${e.seq || e.ts}`]);
        tool={key:toolKey,call:{name:d.name},route,id:d.id,name:d.name || "工具",version:0,missingCall:true,
          stepKey:wrapper(d.name) ? "" : step?.key || ""};
        task.tools.set(toolKey,tool);
      }
      tool.status=d.ok === false ? "failed" : "done";tool.version++;
      const raw=SessionObserver.cleanText(textOf(d.result ?? d.note ?? d.error ?? "未记录原始响应"));
      tool.result=raw.slice(0,24000);tool.clipped=raw.length>24000;
      tool.note=SessionObserver.resultSummary(d);
      if(d.ok !== false && d.skill?.skill_id)tool.skill=d.skill;
      task.progress=`${SessionObserver.toolName(tool.name)} · ${d.ok === false ? "失败" : "已返回"}${tool.note ? `：${brief(tool.note)}` : ""}`;
      const owner=task.steps.get(tool.stepKey);
      if(owner) {
        const pending=[...task.tools.values()].some(t=>t.stepKey===owner.key && t.status==="running");
        if(!terminal.has(task.status)) {
          owner.status=pending ? (task.activeSteps.get(owner.route)===owner ? "running" : "waiting") :
            [...task.tools.values()].some(t=>t.stepKey===owner.key && t.status==="failed") ? "failed" : "done";
          owner.progress=pending ? "等待其他工具返回" : "本次执行已返回";
        }
      }
    }
    if (e.type === "agent.conclude") {
      if (d.summary) task.conclusions.set(route,textOf(d.summary));
      task.progress="已整理路线结论";
      if(step) {if(!terminal.has(task.status)) step.status="done";step.progress="已整理结论";}
    }
    if (e.type === "agent.error" || e.type === "model.failed") task.progress=brief(d.error) || "执行遇到问题，等待下一步";
    if (e.type === "model.cancelled") task.progress="本次模型调用已取消";
    if(step && ["agent.error","model.failed","model.cancelled"].includes(e.type)) {
      step.status=e.type==="model.cancelled" ? "cancelled" : "failed";
      step.progress=brief(d.error) || task.progress;
    }
    if (/^task\.(done|failed|cancelled|pruned)$/.test(e.type)) {
      task.endEvent=e;task.status=e.type.slice(5);task.waitingReason="";task.waitingReasons?.clear();
      for(const s of task.steps.values()) if(!terminal.has(s.status)) s.status=task.status;
      if(d.conclusions?.length) task.conclusions.clear();
      for (const item of d.conclusions || []) {
        if(item.text) task.conclusions.set(item.route || "",textOf(item.text));
      }
      const summary=d.summary;
      if (typeof summary === "string") task.summary=summary;
      else if (summary && typeof summary === "object") {
        task.summary=textOf(summary.conclusion || summary.summary || "");
        task.stats=summary.routes_total != null ? `路线完成 ${summary.routes_done || 0}/${summary.routes_total}` : "";
      }
      if (d.error) task.summary=textOf(d.error);
      task.progress=statuses[task.status];
    }
    return task;
  }
  // 自然语言进展直接可读；结构化执行决策只展示显式面向用户的说明字段。
  function commentaryText(raw) {
    let text=String(raw || "").trim();
    if (/^\[/.test(text)) return "";
    const structured=/^\{/.test(text) || /^```(?:json)?(?:\s|$)/i.test(text);
    if(structured) {
      try {
        const data=JSON.parse(text.replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/, ""));
        text=[data.progress, data.commentary].filter(v=>typeof v === "string" && v.trim()).join("\n\n");
      } catch { return ""; } // JSON 尚未完整时不把半截协议刷成进度。
    }
    return text;
  }
  function updatesOf(task) {
    const seen=new Set(), updates=[];
    for(const [key,raw] of task.texts) {
      const text=commentaryText(raw);
      if(!text || text === task.title || seen.has(text)) continue;
      seen.add(text);updates.push({key,text});
    }
    return updates;
  }
  function summaryOf(task) {
    const label=text=>String(text || "").replace(/[#*_`]/g, "").replace(/\s+/g, "").trim();
    const hasBody=text=>label(text) && label(text)!==label(task.title);
    const conclusions=[...new Set(task.conclusions.values())].filter(hasBody);
    if(hasBody(task.summary) || conclusions.length) return hasBody(task.summary) ? task.summary : conclusions.join("\n\n");
    const latest=updatesOf(task).at(-1)?.text;
    if(latest) return `${statuses[task.status]}${task.stats ? `，${task.stats}` : ""}。\n\n最后一次进展：\n${latest}`;
    const tools=[...task.tools.values()];
    const results=tools.filter(t=>t.status === "done").length, failures=tools.filter(t=>t.status === "failed").length;
    return `${statuses[task.status]}${task.stats ? `，${task.stats}` : ""}。`+
      (tools.length ? `本页记录了 ${tools.length} 次工具调用，${results} 次返回、${failures} 次失败。` : "")+
      "当前记录未包含文字结论。";
  }
  function stageSummary(task, step) {
    const notes=[...new Set([...step.texts.values()].map(commentaryText).filter(Boolean))];
    if(!notes.length && terminal.has(step.status)) return `本阶段${statuses[step.status]}，记录中未包含文字总结，可展开执行明细。`;
    return notes.at(-1) || (step.turn ? "本阶段尚未收到文字进展，正在执行验证。" : "尚未形成阶段结论，执行明细可展开查看。");
  }
  function liveSummary(task) {
    const text=updatesOf(task).at(-1)?.text;
    if(terminal.has(task.status)) return summaryOf(task);
    return text || (task.title ? `正在处理：${task.title}。尚未收到阶段结论。` : "正在准备任务，尚未收到阶段进展。");
  }
  let store=create();
  function skillsOf(task) {
    const skills=new Map();
    for(const tool of task.tools.values()) {
      if(tool.status !== "done" || !tool.skill?.skill_id)continue;
      const skill=tool.skill,key=JSON.stringify([skill.skill_id,skill.version,skill.revision]);
      if(!skills.has(key))skills.set(key,{id:skill.skill_id,version:skill.version,paths:new Set()});
      if(skill.path)skills.get(key).paths.add(skill.path);
    }
    return [...skills.values()].map(skill=>({...skill,paths:[...skill.paths]}));
  }
  const rows=new Map(), stepRows=new Map(), summaryRows=new Map(), dirty=new Set();
  let timer=null;
  const stepId=(task,step)=>JSON.stringify([task.key,step.key]);
  function stepTitle(task,step) {
    const text=[...step.texts.values()].map(commentaryText).find(Boolean);
    if(text) return brief(text.split(/\n|[。！？]/)[0]).slice(0,100);
    if(step.title && step.title!==task.title) return brief(step.title);
    const names=[...new Set([...task.tools.values()].filter(t=>t.stepKey===step.key).map(t=>SessionObserver.toolName(t.name)))];
    return names.join("、") || "分析当前现场";
  }
  function renderTools(task,tools,list) {
    for(const tool of tools) {
      let el=[...list.children].find(child=>child.dataset.toolKey === tool.key);
      if(!el) {
        list.insertAdjacentHTML("beforeend",`<div class="tl-act"><button class="hd" type="button" aria-expanded="false"><span class="nm"></span><span class="ar"></span><span class="st"></span><span class="cv" aria-hidden="true">⌄</span></button><div class="nt"></div><pre class="tl-output"></pre></div>`);
        el=list.lastElementChild;el.dataset.toolKey=tool.key;
        const button=el.querySelector("button.hd");
        button.onclick=()=>{
          const open=el.classList.toggle("open");button.setAttribute("aria-expanded",String(open));
          const parameters=el.querySelector(".tl-parameters");if(parameters) parameters.open=open;
        };
        if(!tool.missingCall) ToolParameters.attach(el,tool.call);
      }
      if(el.dataset.version === String(tool.version)) continue;
      el.dataset.version=String(tool.version);
      el.querySelector(".nm").textContent=SessionObserver.toolName(tool.name);
      el.querySelector(".ar").textContent=tool.missingCall ? "调用参数见更早记录" : tool.call.preview || argBrief(tool.name || "",tool.call.arguments);
      el.querySelector(".st").textContent=tool.status === "running" ? "进行中" : tool.status === "failed" ? "失败" : "已返回";
      el.querySelector(".st").className=`st ${tool.status === "failed" ? "n" : tool.status === "done" ? "y" : ""}`;
      el.querySelector(".nt").textContent=tool.note || "";
      el.querySelector(".tl-output").textContent=(tool.result || "等待工具返回")+(tool.clipped ? "\n\n此处展示前 24,000 字，完整内容可导出审计查看。" : "");
    }
  }
  function render(task) {
    let row=rows.get(task.key);
    if(!row?.isConnected) {
      row=traceAppend(task.event,`<section class="task-thread task-dispatch" aria-label="子任务派发">
        <header class="task-heading"><span class="task-marker" aria-hidden="true"></span><strong></strong><span class="task-status"></span></header>
        <p class="task-context"></p><div class="task-live-summary md" aria-live="polite"></div><p class="task-waiting" hidden></p><p class="task-skills" hidden></p>
        <div class="task-commentary md"></div>
        <details class="task-activity"><summary>其他执行记录</summary><div class="task-activity-body"><div class="task-tools"></div></div></details>
      </section>`,"tl-task");
      rows.set(task.key,row);row.dataset.taskId=task.id;row.dataset.taskPart="dispatch";
    }
    const root=row.querySelector(".task-thread");
    root.querySelector(".task-heading strong").textContent=task.title || `${actorName(task.actor)} · ${task.id || "当前记录片段"}`;
    root.dataset.status=task.status;
    root.querySelector(".task-status").textContent=task.waitingReason ? "等待补充信息" : (statuses[task.status] || "执行中");
    const live=root.querySelector(".task-live-summary"), liveText=liveSummary(task);
    if(live.dataset.text!==liveText) {live.dataset.text=liveText;live.innerHTML=mdRender(liveText);}
    const waiting=root.querySelector(".task-waiting");
    waiting.hidden=!task.waitingReason || terminal.has(task.status);
    waiting.textContent=task.waitingReason ? `等待：${task.waitingReason}` : "";
    root.querySelector(".task-context").textContent=[actorName(task.actor),task.target || task.id].filter(Boolean).join(" · ");
    const skillLine=root.querySelector(".task-skills"),skills=skillsOf(task);
    skillLine.hidden=!skills.length;skillLine.textContent=skills.map(skill=>`使用技能「${skill.id}」· v${skill.version} · 已读 ${skill.paths.length} 个文件`).join("；");
    const commentary=root.querySelector(".task-commentary");
    commentary.hidden=task.steps.size>0;
    if(!commentary.hidden) commentary.innerHTML=mdRender(updatesOf(task).map(u=>u.text).join("\n\n"));
    const tools=[...task.tools.values()];
    const other=root.querySelector(".task-activity"), ungrouped=tools.filter(t=>!t.stepKey && (!wrapper(t.name) || !task.steps.size));
    other.hidden=!ungrouped.length;
    renderTools(task,ungrouped,other.querySelector(".task-tools"));
    for(const step of task.steps.values()) {
      const id=stepId(task,step);
      let stepRow=stepRows.get(id);
      if(!stepRow?.isConnected) {
        stepRow=traceAppend(step.event,`<section class="task-thread task-stage" aria-label="推进阶段">
          <p class="task-context"></p>
          <header class="task-heading"><span class="task-marker" aria-hidden="true"></span><strong></strong><span class="task-status"></span></header>
          <div class="task-stage-summary md" aria-live="polite"></div>
          <details class="task-step"><summary>执行明细 <span class="task-tool-count"></span></summary>
            <p class="task-progress"></p><div class="task-tools"></div>
          </details></section>`,"tl-task tl-task-step");
        stepRows.set(id,stepRow);stepRow.dataset.taskId=task.id;stepRow.dataset.stepKey=step.key;
      }
      const stage=stepRow.querySelector(".task-stage"),group=stage.querySelector(".task-step");
      group.dataset.stepKey=step.key;
      stage.dataset.status=step.status;
      // 工具默认折叠，用户展开后保留；阶段说明始终可见。
      group.dataset.status=step.status;
      stage.querySelector(".task-context").textContent=[task.title || task.id,step.route,step.turn ? `第 ${step.turn} 次推进` : ""].filter(Boolean).join(" · ");
      stage.querySelector(".task-heading strong").textContent=stepTitle(task,step);
      stage.querySelector(".task-status").textContent=statuses[step.status] || "执行中";
      group.querySelector(".task-progress").textContent=step.progress || "";
      const text=stageSummary(task,step), target=stage.querySelector(".task-stage-summary");
      if(target.dataset.text!==text) {target.dataset.text=text;target.innerHTML=mdRender(text);}
      const owned=tools.filter(t=>t.stepKey===step.key), list=group.querySelector(".task-tools");
      list.hidden=!owned.length;
      group.querySelector(".task-tool-count").textContent=owned.length ? `· ${owned.filter(t=>t.status!=="running").length}/${owned.length} 次返回` : "· 暂无工具调用";
      renderTools(task,owned,list);
    }
    if(terminal.has(task.status)) {
      let summary=summaryRows.get(task.key);
      if(!summary?.isConnected) {
        summary=traceAppend(task.endEvent || task.event,`<section class="task-thread task-outcome" aria-label="子任务结论"><header class="task-heading"><span class="task-marker" aria-hidden="true"></span><strong></strong><span class="task-status"></span></header><div class="task-summary md"></div></section>`,"tl-task");
        summaryRows.set(task.key,summary);summary.dataset.taskId=task.id;summary.dataset.taskPart="summary";
      }
      const outcome=summary.querySelector(".task-thread");outcome.dataset.status=task.status;
      outcome.querySelector("strong").textContent=`${task.title || task.id || actorName(task.actor)} · 子任务结论`;
      outcome.querySelector(".task-status").textContent=statuses[task.status];
      outcome.querySelector(".task-summary").innerHTML=mdRender(summaryOf(task));
    }
    traceScroll();
    if (S.tab === "topo" && typeof renderTopoActivity === "function") renderTopoActivity();
  }
  function push(e) {
    const task=reduce(store,e);
    if(!task) return false;
    if(e.type === "agent.delta" && e.detail?.kind === "reasoning") {
      const rec=S.think.get(e.actor) || {chars:0,tail:""};
      const text=e.detail.text || "";
      rec.chars+=text.length;rec.tail=(rec.tail+text).slice(-6000);S.think.set(e.actor,rec);
    }
    if(!rows.get(task.key)?.isConnected || [...task.steps.values()].some(step=>!stepRows.get(stepId(task,step))?.isConnected)
        || terminal.has(task.status) && !summaryRows.get(task.key)?.isConnected) render(task);
    else {
      dirty.add(task.key);
      if(timer === null) timer=setTimeout(()=>{
        timer=null;
        for(const key of dirty) {const current=store.tasks.get(key);if(current) render(current);}
        dirty.clear();
      },100);
    }
    // 与历史窗口一起释放已移出页面的任务，不影响仍在阅读的折叠块。
    if(rows.size>250) for(const [key,row] of rows) if(!row.isConnected) {
      if([...taskSteps(key)].some(row=>row.isConnected) || summaryRows.get(key)?.isConnected) continue;
      rows.delete(key);store.tasks.delete(key);
      for(const [id,row] of stepRows) if(!row.isConnected) stepRows.delete(id);
      summaryRows.delete(key);}
    return true;
  }
  const taskSteps=key=>[...(store.tasks.get(key)?.steps.values() || [])].map(step=>stepRows.get(JSON.stringify([key,step.key]))).filter(Boolean);
  function routeSnapshot() {
    return [...store.tasks.values()].flatMap(task=>[...task.steps.values()].flatMap(step=>{
      const text=[...step.texts.values()].map(commentaryText).filter(Boolean).at(-1);
      return text ? [{id:`__process_route:${JSON.stringify([task.key,step.key])}`, kind:"milestone",
        title:brief(text.split(/\n/)[0]), body:text, task_id:task.id, route_id:step.route,
        turn:step.turn, actor:task.actor, status:step.status, evidence_refs:[], parent_finding_id:""}] : [];
    }));
  }
  function progressSnapshot() {
    return [...store.tasks.values()].filter(task=>task.id).map(task=>({
      task_id:task.id, actor:task.actor, title:task.title, status:task.status,
      text:liveSummary(task), waiting:task.waitingReason || "", updated:task.updated}));
  }
  function reset() {if(timer !== null) clearTimeout(timer);timer=null;dirty.clear();store=create();rows.clear();stepRows.clear();summaryRows.clear();}
  function flush() {
    if(timer !== null) clearTimeout(timer);
    timer=null;
    for(const key of dirty) {const task=store.tasks.get(key);if(task) render(task);}
    dirty.clear();
  }
  return {push,reset,flush,create,reduce,summaryOf,resolve,updatesOf,commentaryText,skillsOf,stepTitle,stageSummary,liveSummary,progressSnapshot,routeSnapshot};
})();
