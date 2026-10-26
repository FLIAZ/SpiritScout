"use strict";
/* 子任务按身份聚合；历史回放和实时事件共用，折叠状态不随增量更新重置。 */
const TaskTrace = (() => {
  const terminal = new Set(["done", "failed", "cancelled", "pruned"]);
  const statuses = {running:"执行中", done:"已完成", failed:"执行失败", cancelled:"已撤销", pruned:"已暂缓"};
  const supported = /^(task\.(start|done|failed|cancelled|pruned)|agent\.(turn|delta|tool_call|tool_result|conclude|note|error)|model\.(started|call|failed|cancelled))$/;
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
        texts:new Map(), conclusions:new Map(), summary:"", updated:e.ts, event:e, version:0};
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
    const route=d.route_id || d.route || "";
    const stem=JSON.stringify([route,d.call_id || d.turn || ""]);
    if (e.type === "task.start") { task.status="running";task.progress="开始执行"; }
    if (e.type === "agent.turn") task.progress=`正在推进${d.turn ? ` · 第 ${d.turn} 步` : ""}`;
    if (e.type === "model.started") task.progress="正在分析下一步";
    if (e.type === "agent.delta") {
      if (d.kind === "reset") task.texts.delete(stem);
      else if (d.kind !== "reasoning" && d.kind !== "tool_call" && d.text) {
        const text=(task.texts.get(stem) || "")+d.text;
        task.texts.set(stem,text.slice(-24000)); task.progress="正在更新进展";
      }
    }
    if (e.type === "agent.note") {task.progress="已更新进展";task.texts.set(`note:${e.seq || e.ts}`,textOf(d.text));}
    if (e.type === "agent.tool_call") {
      for (const c of d.calls || []) {
        const toolKey=JSON.stringify([route,c.id || `${e.seq || e.ts}:${task.tools.size}`]);
        task.tools.set(toolKey,{key:toolKey,call:c,route,id:c.id,name:c.name,status:"running",result:"",note:"",version:1});
      }
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
        tool={key:toolKey,call:{name:d.name},route,id:d.id,name:d.name || "工具",version:0,missingCall:true};
        task.tools.set(toolKey,tool);
      }
      tool.status=d.ok === false ? "failed" : "done";tool.version++;
      const raw=SessionObserver.cleanText(textOf(d.result ?? d.note ?? d.error ?? "未记录原始响应"));
      tool.result=raw.slice(0,24000);tool.clipped=raw.length>24000;
      tool.note=SessionObserver.resultSummary(d);
      task.progress=`${SessionObserver.toolName(tool.name)} · ${d.ok === false ? "失败" : "已返回"}${tool.note ? `：${brief(tool.note)}` : ""}`;
    }
    if (e.type === "agent.conclude") {
      if (d.summary) task.conclusions.set(route,textOf(d.summary));
      task.progress="已整理路线结论";
    }
    if (e.type === "agent.error" || e.type === "model.failed") task.progress=brief(d.error) || "执行遇到问题，等待下一步";
    if (e.type === "model.cancelled") task.progress="本次模型调用已取消";
    if (/^task\.(done|failed|cancelled|pruned)$/.test(e.type)) {
      task.status=e.type.slice(5);
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
  let store=create();
  const rows=new Map(), dirty=new Set();
  let timer=null;
  function render(task) {
    let row=rows.get(task.key);
    if(!row?.isConnected) {
      row=traceAppend(task.event,`<section class="task-thread" aria-label="子任务进度">
        <header class="task-heading"><span class="task-marker" aria-hidden="true"></span><strong></strong><span class="task-status"></span></header>
        <div class="task-commentary"></div>
        <details class="task-narrative" hidden><summary>查看完整进展</summary><div class="md"></div></details>
        <p class="task-progress"></p>
        <details class="task-activity"><summary><span>执行详情</span><span class="task-count"></span><span class="task-errors"></span></summary>
          <div class="task-activity-body"><div class="task-tools"></div></div>
        </details>
        <section class="task-summary" hidden><h4>子任务摘要</h4><div class="md"></div></section>
      </section>`,"tl-task");
      rows.set(task.key,row);
      row.dataset.taskId=task.id;
      const narrative=row.querySelector(".task-narrative");
      narrative.addEventListener("toggle",()=>{
        if(narrative.open) narrative.querySelector(".md").innerHTML=mdRender(updatesOf(task).map(u=>u.text).join("\n\n"));
      });
    }
    const root=row.querySelector(".task-thread");
    root.dataset.status=task.status;
    root.querySelector(".task-heading strong").textContent=task.title || `${actorName(task.actor)}${task.id ? ` · ${task.id}` : " · 当前记录片段"}`;
    root.querySelector(".task-status").textContent=statuses[task.status];
    root.querySelector(".task-progress").textContent=task.progress;
    const tools=[...task.tools.values()], failed=tools.filter(t=>t.status === "failed").length;
    const pending=tools.filter(t=>t.status === "running").length;
    root.querySelector(".task-count").textContent=`${tools.length} 次工具调用${pending ? ` · ${pending} 次进行中` : ""}`;
    root.querySelector(".task-errors").textContent=failed ? `${failed} 次失败` : "";
    const updates=updatesOf(task), visible=updates.slice(-3);
    const commentary=root.querySelector(".task-commentary");
    const signature=JSON.stringify(visible);
    if(commentary.dataset.signature !== signature) {
      commentary.dataset.signature=signature;
      commentary.innerHTML=visible.map(update=>{
        const clipped=update.text.length>600;
        return `<article class="task-update md">${mdRender(clipped ? update.text.slice(0,600)+"…" : update.text)}</article>`;
      }).join("");
    }
    const narrative=root.querySelector(".task-narrative");
    const narrativeText=updates.map(u=>u.text).join("\n\n");
    narrative.hidden=updates.length<=3 && visible.every(u=>u.text.length<=600);
    narrative.querySelector("summary").textContent=`查看完整进展（${updates.length} 条）`;
    if(narrative.dataset.text !== narrativeText) {
      narrative.dataset.text=narrativeText;
      if(narrative.open) narrative.querySelector(".md").innerHTML=mdRender(narrativeText);
    }
    const list=root.querySelector(".task-tools");
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
    const summary=root.querySelector(".task-summary");
    summary.hidden=!terminal.has(task.status);
    if(!summary.hidden) summary.querySelector(".md").innerHTML=mdRender(summaryOf(task));
    traceScroll();
  }
  function push(e) {
    const task=reduce(store,e);
    if(!task) return false;
    if(e.type === "agent.delta" && e.detail?.kind === "reasoning") {
      const rec=S.think.get(e.actor) || {chars:0,tail:""};
      const text=e.detail.text || "";
      rec.chars+=text.length;rec.tail=(rec.tail+text).slice(-6000);S.think.set(e.actor,rec);
    }
    if(!rows.get(task.key)?.isConnected) render(task);
    else {
      dirty.add(task.key);
      if(timer === null) timer=setTimeout(()=>{
        timer=null;
        for(const key of dirty) {const current=store.tasks.get(key);if(current) render(current);}
        dirty.clear();
      },100);
    }
    // 与历史窗口一起释放已移出页面的任务，不影响仍在阅读的折叠块。
    if(rows.size>250) for(const [key,row] of rows) if(!row.isConnected) {rows.delete(key);store.tasks.delete(key);}
    return true;
  }
  function reset() {if(timer !== null) clearTimeout(timer);timer=null;dirty.clear();store=create();rows.clear();}
  return {push,reset,create,reduce,summaryOf,resolve,updatesOf,commentaryText};
})();
