"use strict";
/* 会话页流式正文定格：模型调用收尾（model.call / model.failed / model.cancelled）
   必须立刻把 .tl-say 原文替换为 Markdown 渲染，不能等下一次回合边界——
   否则纯推理续轮期间（无新 agent.turn/tool_call/conclude）md 原文会长时间留在时间线。 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../src/spiritscout/web/static/js/views.js"), "utf8");
const cut = (from, to) => source.slice(source.indexOf(from), source.indexOf(to));
const context = vm.createContext(require('./helpers/i18n').context({}));
vm.runInContext(
  fs.readFileSync(path.join(__dirname, "../src/spiritscout/web/static/js/markdown.js"), "utf8"),
  context);

vm.runInContext(fs.readFileSync(path.join(__dirname, "../src/spiritscout/web/static/js/task-trace.js"), "utf8"), context);

vm.runInContext("TaskTrace.push = () => false", context); // 单独验证旧轨迹视图，解析器仍用真实实现。

const ACTOR = "business-context-d6ca02073cf8";
const mkSay = () => {
  const el = { kind: "say", textContent: "", innerHTML: "", dataset: {} };
  const cls = new Set(["tl-say"]);
  el.classList = { add: c => cls.add(c), remove: c => cls.delete(c),
                   contains: c => cls.has(c), toggle: c => cls.has(c) ? (cls.delete(c), false) : (cls.add(c), true) };
  return el;
};
const children = [];
const mkThink = () => {
  const el=mkSay();el.kind="think";el.classList.add("tl-think");el.classList.add("live");
  const summary={textContent:"思考中"},body={lastChild:null,append(text){this.lastChild=text;}};
  el.querySelector=selector=>selector==="summary" ? summary : selector===".bd" ? body : null;
  return el;
};
const flowEl = {
  get lastElementChild() { return children[children.length - 1] || null; },
  querySelector(sel) {
    if (sel === ".tl-think.live") return children.find(c=>c.kind==="think" && c.classList.contains("live")) || null;
    if (sel === ".tl-thinkchip") return children.find(c => c.kind === "chip") || null;
    if (sel === ".tl-say:last-child") {
      const last = children[children.length - 1];
      return last && last.kind === "say" ? last : null;
    }
    return null;
  },
  insertAdjacentHTML(_pos, html) { children.push(html.includes("tl-thinkchip") ? { kind: "chip", textContent: "" } : html.includes('tl-think live') ? mkThink() : mkSay()); },
};
const turnEl = {
  isConnected: true,
  dataset: { agent: ACTOR },
  querySelector: sel => sel === ".flow" ? flowEl : null,
  querySelectorAll: sel => {
    if (sel === ".tl-think.live") return children.filter(c=>c.kind==="think" && c.classList.contains("live"));
    if (sel === ".tl-say:not(.md)") return children.filter(c => c.kind === "say" && !c.classList.contains("md"));
    return [];
  },
};
const box = { querySelectorAll: sel => sel === ".tl-turn" ? [turnEl] : [], querySelector: () => null };
const appendedRows=[];
const observedEvents=[];
Object.assign(context, {
  traceRenderTarget: null,
  traceIsPresent: row => !!row?.isConnected,
  TRACE_TURNS: new Map([[ACTOR, turnEl]]),
  $: () => box, esc: v => String(v ?? ""), actorName: a => String(a), agentName: a => String(a), fmtK: n => String(n),
  fmtElapsed: n => String(n), fmtSize: n => String(n), TERMINAL: new Set(),
  S: { state: null, think: new Map() },
  document:{createTextNode(text){return {nodeType:3,data:text,appendData(value){this.data+=value;}};}},
  STEERING_STATUS_CN: {completed:"已报告完成"},
  SessionObserver: { push: e => observedEvents.push(e), usageFor: () => 5, toolName: n => String(n), resultSummary: () => "", cleanText: v => String(v ?? "") },
  AgentWorkbench: { push: () => {} },
  ToolParameters: { attach: () => {} },
  traceAppend: (event,body,cls="") => {appendedRows.push({event,body,cls});return {dataset:{}};}, traceScroll: () => {},
});
const core = fs.readFileSync(path.join(__dirname, "../src/spiritscout/web/static/js/core.js"), "utf8");
Object.assign(context, {atob, TextDecoder, Uint8Array});
vm.runInContext(core.slice(core.indexOf("function modelDisplayName("), core.indexOf("async function api(")), context);
vm.runInContext(cut("function traceTurn(", "function argBrief(") + cut("function traceRetryStatus(", 'const traceBox = $("#trace-box");'), context);

const push = (type, detail) => vm.runInContext("tracePush", context)({ type, actor: ACTOR, detail, ts: "2026-10-17T00:00:00Z" });
const say = () => children.filter(c => c.kind === "say");

push("model.started", {call_id:"usage-only",model:"example",provider_ref:"provider"});
push("model.waiting", {call_id:"usage-only",elapsed_ms:3000});
push("model.call", {call_id:"usage-only",tokens:47396,latency_ms:7800});
assert.equal(appendedRows.length,0,"纯模型调用与计量事件不占用正文位置");
assert.deepEqual(observedEvents.slice(-3).map(e=>e.type),["model.started","model.waiting","model.call"],"实况继续收到完整调用事件");

push("agent.delta", { kind: "reasoning", text: "先构思结论文本结构。" });
push("agent.delta", { kind: "text", text: "**结论文本（供系统归纳业务上下文）**" });
push("agent.delta", { kind: "text", text: "\n\n## 一、已观测事实\n- 游客态只读走访" });
assert.equal(say().length, 1);
assert.equal(say()[0].classList.contains("md"), false, "流式期间保持原文，不逐字渲染");
assert.equal(say()[0].textContent.includes("**结论文本"), true);

push("model.call", { call_id: "call-1", model: "repro-model", latency_ms: 100 });
assert.equal(say()[0].classList.contains("md"), true, "调用收尾立即定格 Markdown");
assert.match(say()[0].innerHTML, /<b>结论文本（供系统归纳业务上下文）<\/b>/);
assert.match(say()[0].innerHTML, /<h2>一、已观测事实<\/h2>/);
assert.equal(say()[0].innerHTML.includes("**"), false);

push("agent.delta", { kind: "text", text: "续写的第二段" });
assert.equal(say().length, 2, "收尾之后的新增文本另起新块");
push("model.cancelled", { call_id: "call-2" });
assert.equal(say()[1].classList.contains("md"), true, "取消的调用也要定格已流入正文");
push("agent.delta", { kind: "text", text: "失败前流入的正文" });
push("model.failed", { call_id: "call-3", error: "超时" });
assert.equal(say()[2].classList.contains("md"), true, "失败的调用也要定格已流入正文");
assert.ok(appendedRows.some(row=>row.body.includes("超时")),"调用错误仍在正文中可见");

push("ring.steer", {agent:"recon",hint:"请先核对 **登录入口**：\n\n- 记录响应头\n- 不提交凭据"});
const steering=appendedRows.at(-1);
assert.match(steering.body,/class="tl-reply md"/);
assert.match(steering.body,/<b>登录入口<\/b>/);
assert.match(steering.body,/<ul>/,"督导意见使用与子 Agent 正文一致的 Markdown 排版");
assert.equal(steering.body.includes("**登录入口**"),false);

push("ring.steer_feedback", {status:"completed",detail:"已完成：\n\n`GET /login` 返回 302。"});
const receipt=appendedRows.at(-1);
assert.match(receipt.body,/class="tl-reply md"/);
assert.match(receipt.body,/<code>GET \/login<\/code>/,"督导回执不再落入无样式纯文本容器");
assert.equal(receipt.body.includes('class="m"'),false);
console.log("trace stream markdown: model call boundaries finalize streamed text into rendered markdown passed");

// 混合正文和分片标注在流式阶段与 Markdown 收尾都不能泄漏协议。
push("agent.delta", {kind:"text", text:'已观察到登录表单。\n\n{"pro'});
assert.equal(say().at(-1).textContent.trim(), '已观察到登录表单。');
push("agent.delta", {kind:"text", text:'gress":"继续验证","exploration":{"intent":"核实入口","attempt":"读取表单"}}'});
push("model.call", {call_id:"mixed"});
assert.ok(!say().at(-1).innerHTML.includes('exploration'));
assert.ok(!say().at(-1).innerHTML.includes('progress'));
assert.ok(say().at(-1).innerHTML.includes('已观察到登录表单'));

context.S.state={session:{mode:'src'}};
context.TRACE_TURNS.set('master-src',turnEl);
const masterPush=(type,detail)=>vm.runInContext('tracePush',context)({type,actor:'master-src',detail});
masterPush('agent.delta',{kind:'reasoning',text:'核对当前事实。'});
const thinking=children.find(c=>c.kind==='think'),body=thinking.querySelector('.bd');
const node=body.lastChild;
masterPush('agent.delta',{kind:'reasoning',text:'再评估下一步。'});
assert.equal(body.lastChild,node,'增量追加思考保留已有正文节点及用户选区');
assert.equal(node.data,'核对当前事实。再评估下一步。','主 Agent 的已有流式推理可展开阅读');
masterPush('model.call',{call_id:'master-think'});
assert.equal(thinking.classList.contains('live'),false);
assert.equal(thinking.querySelector('summary').textContent,'思考过程');
assert.equal(body.lastChild,node,'调用结束保留已展示的思考内容');
for (const mode of ['ctf','competition']) {
  context.S.state.session.mode=mode;
  masterPush('agent.delta',{kind:'reasoning',call_id:`${mode}-a`,text:'先核对事实。'});
  masterPush('agent.delta',{kind:'reasoning',call_id:`${mode}-b`,text:'另一条调用。'});
  masterPush('model.call',{call_id:`${mode}-a`});
  const calls=children.filter(c=>c.kind==='think'&&c.dataset.callId?.startsWith(mode));
  assert.equal(calls.length,2);
  assert.equal(calls[0].classList.contains('live'),false);
  assert.equal(calls[1].classList.contains('live'),true);
  masterPush('model.cancelled',{call_id:`${mode}-b`});
  assert.equal(calls[1].classList.contains('live'),false);
}
console.log('Master thinking: SRC, CTF, competition, stable text nodes and call-scoped completion passed');
