"use strict";
/* 每个实例独立聚合，SSE 历史回放与实时增量共用，所有文本均按纯文本展示。 */
const AgentWorkbench = (() => {
  const create = () => ({agents: new Map(), selected: ""});
  const worker = actor => !!actor && !/^(master(?:-|$)|gateway$|runtime$|system$|user$)/.test(actor);
  const limit = 24000;
  function reduce(store, event) {
    const actor = String(event.actor || ""), d = event.detail || {}, type = event.type || "";
    if (!worker(actor) || !/^(agent\.|task\.|model\.)/.test(type)) return;
    if (!/^(agent\.(turn|delta|tool_call|tool_result|conclude|note|error|ask_user|ask_answered)|task\.(start|done|failed|cancelled|pruned)|model\.(started|call|failed|cancelled))$/.test(type)) return;
    let agent = store.agents.get(actor);
    if (!agent) {
      agent = {actor, status:"等待事件", turn:0, blocks:[], tools:new Map(), calls:new Set(), updated:event.ts};
      store.agents.set(actor, agent);
      if (!store.selected) store.selected = actor;
    }
    agent.updated = event.ts;
    const append = (kind, text, key = "") => {
      if (!text) return;
      let block = key && agent.blocks.find(b => b.key === key);
      if (!block) {
        block = {kind, text:"", key, ts:event.ts, route:d.route || d.route_id || ""};
        agent.blocks.push(block);
      }
      const joined = block.text + String(text);
      block.clipped = block.clipped || joined.length > limit;
      block.text = joined.slice(-limit);
      if (agent.blocks.length > 80) agent.blocks.shift();
    };
    if (type === "task.start") { agent.status="执行中"; agent.target=d.target || ""; }
    if (type === "agent.turn") { agent.turn=d.turn || 0; agent.status="执行中"; append("进展",d.angle); }
    if (type === "model.started") { agent.calls.add(d.call_id); agent.status="模型生成中"; }
    if (/^model\.(call|failed|cancelled)$/.test(type)) {
      agent.calls.delete(d.call_id);
      agent.status=agent.calls.size ? "模型生成中" : type === "model.call" ? "执行中" : "调用异常";
      if (d.error) append("错误", d.error);
    }
    if (type === "agent.delta") {
      const stem = d.call_id || `${d.route || ""}:${d.turn || agent.turn}`;
      if (d.kind === "reset") {
        agent.blocks = agent.blocks.filter(b => !b.key.startsWith(stem+":"));
      } else if (d.kind !== "tool_call") {
        append(d.kind === "reasoning" ? "分析" : "输出", d.text, `${stem}:${d.kind || "text"}`);
      }
    }
    if (type === "agent.tool_call") {
      for (const c of d.calls || []) agent.tools.set(c.id, {name:c.name, status:"执行中", text:c.preview || "等待工具返回", ts:event.ts});
      agent.status="工具执行中";
    }
    if (type === "agent.tool_result") {
      const tool=agent.tools.get(d.id) || {name:d.name || "工具反馈"};
      Object.assign(tool,{status:d.ok === false ? "失败" : "已返回",text:SessionObserver.resultSummary(d),ts:event.ts, raw:SessionObserver.cleanText(typeof d.result === "object" ? JSON.stringify(d.result,null,2) : d.result || d.note || d.error).slice(-limit)});
      agent.tools.set(d.id,tool);
      agent.status=agent.calls.size ? "模型生成中" : "执行中";
    }
    if (type === "agent.note") append("进展",d.text);
    if (type === "agent.conclude") append("结论",d.summary);
    if (type === "agent.error") { append("错误",d.error);agent.status="执行异常"; }
    if (type === "agent.ask_user") { append("待回答",d.question);agent.status="等待你的回答"; }
    if (type === "agent.ask_answered") agent.status="执行中";
    const statuses={"task.done":"已完成","task.failed":"失败","task.cancelled":"已撤销","task.pruned":"已剪枝"};
    if (statuses[type]) { agent.status=statuses[type];agent.calls.clear(); }
    while (agent.tools.size > 50) agent.tools.delete(agent.tools.keys().next().value);
  }
  let store=create(), timer=null;
  function render() {
    timer=null;
    const root=$("#agent-workbench"); if (!root) return;
    const agents=[...store.agents.values()], selected=store.agents.get(store.selected);
    const nav=root.querySelector(".aw-agents"), pane=root.querySelector(".aw-pane");
    root.querySelector(".aw-count").textContent=`${agents.length} 个实例`;
    const navSignature=JSON.stringify(agents.map(a=>[a.actor,a.status,a.turn,store.selected]));
    if(nav.dataset.signature !== navSignature) {
      const focus=document.activeElement?.dataset.awActor, offset=nav.scrollLeft;
      nav.dataset.signature=navSignature;
      nav.innerHTML=agents.map(a=>`<button type="button" data-aw-actor="${esc(a.actor)}" aria-pressed="${a.actor===store.selected}" class="aw-agent ${a.actor===store.selected ? "selected" : ""}"><strong>${esc(actorName(a.actor))}</strong><small>${esc(a.status)}${a.turn ? ` · 第 ${esc(a.turn)} 回合` : ""}</small><span>${esc(a.actor)}</span></button>`).join("");
      nav.scrollLeft=offset;
      if(focus) [...nav.querySelectorAll("button")].find(b=>b.dataset.awActor===focus)?.focus({preventScroll:true});
    }
    if (!selected) { pane.innerHTML='<p class="aw-empty">尚未派发子 Agent。派发后可在这里按角色查看分析、工具反馈与产出。</p>';return; }
    const bodySignature=JSON.stringify([selected, [...selected.tools], [...selected.calls]]);
    if (pane.dataset.signature===bodySignature) return;
    const same=pane.dataset.actor===selected.actor;
    const opened=new Set([...pane.querySelectorAll("details[open]")].map(el=>el.dataset.awTool));
    const oldScroll=pane.scrollTop, stick=pane.scrollHeight-pane.scrollTop-pane.clientHeight<40;
    pane.dataset.signature=bodySignature;pane.dataset.actor=selected.actor;
    const blocks=selected.blocks;
    const blockHtml=b=>`<article class="aw-entry ${b.kind === "错误" ? "bad" : ""}"><header><b>${esc(b.kind)}</b><time>${esc(evElapsed(b.ts))}</time>${b.route ? `<small>${esc(b.route)}</small>` : ""}</header><pre>${esc(b.text)}</pre>${b.clipped ? '<small>此块仅展示最近 24,000 字，完整记录可通过「导出审计」下载。</small>' : ""}</article>`;
    pane.innerHTML=`<div class="aw-columns"><section><h4>实时分析与输出</h4>${blocks.filter(b=>["分析","输出"].includes(b.kind)).map(blockHtml).join("") || '<p class="aw-empty">尚未收到模型输出。旧记录或未返回流式文本的端点可能没有分析正文。</p>'}</section><section><h4>执行反馈与产出</h4>${blocks.filter(b=>!["分析","输出"].includes(b.kind)).map(blockHtml).join("")}${[...selected.tools.entries()].map(([id,t])=>`<article class="aw-entry ${t.status === "失败" ? "bad" : ""}"><header><b>${esc(SessionObserver.toolName(t.name))}</b><span>${esc(t.status)}</span></header><pre>${esc(t.text)}</pre>${t.raw && t.raw !== t.text ? `<details data-aw-tool="${esc(String(id))}"><summary>展开工具反馈（最多 24,000 字）</summary><pre>${esc(t.raw)}</pre></details>` : ""}</article>`).join("") || '<p class="aw-empty">等待工具反馈与结论</p>'}</section></div>`;
    if(same) for(const el of pane.querySelectorAll("details")) if(opened.has(el.dataset.awTool)) el.open=true;
    pane.scrollTop=same && !stick ? oldScroll : pane.scrollHeight;
  }
  function push(event) { reduce(store,event); if (timer===null) timer=setTimeout(render,120); }
  function reset() { if(timer!==null) clearTimeout(timer); timer=null;store=create();const root=$("#agent-workbench");if(root){for(const el of root.querySelectorAll("[data-signature]"))delete el.dataset.signature;}render(); }
  function init() {
    const root=$("#agent-workbench"); if(!root)return;
    root.addEventListener("click",event=>{const b=event.target.closest("[data-aw-actor]");if(!b)return;store.selected=b.dataset.awActor;render();});
    render();
  }
  return {push,reset,render,init,create,reduce};
})();
