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
const context = vm.createContext({});
vm.runInContext(
  fs.readFileSync(path.join(__dirname, "../src/spiritscout/web/static/js/markdown.js"), "utf8"),
  context);

const ACTOR = "business-context-d6ca02073cf8";
const mkSay = () => {
  const el = { kind: "say", textContent: "", innerHTML: "", dataset: {} };
  const cls = new Set(["tl-say"]);
  el.classList = { add: c => cls.add(c), remove: c => cls.delete(c),
                   contains: c => cls.has(c), toggle: c => cls.has(c) ? (cls.delete(c), false) : (cls.add(c), true) };
  return el;
};
const children = [];
const flowEl = {
  get lastElementChild() { return children[children.length - 1] || null; },
  querySelector(sel) {
    if (sel === ".tl-thinkchip") return children.find(c => c.kind === "chip") || null;
    if (sel === ".tl-say:last-child") {
      const last = children[children.length - 1];
      return last && last.kind === "say" ? last : null;
    }
    return null;
  },
  insertAdjacentHTML(_pos, html) { children.push(html.includes("tl-thinkchip") ? { kind: "chip", textContent: "" } : mkSay()); },
};
const turnEl = {
  isConnected: true,
  dataset: { agent: ACTOR },
  querySelector: sel => sel === ".flow" ? flowEl : null,
  querySelectorAll: sel => {
    if (sel === ".tl-say:not(.md)") return children.filter(c => c.kind === "say" && !c.classList.contains("md"));
    return [];
  },
};
const box = { querySelectorAll: sel => sel === ".tl-turn" ? [turnEl] : [], querySelector: () => null };
Object.assign(context, {
  traceRenderTarget: null,
  TRACE_TURNS: new Map([[ACTOR, turnEl]]),
  $: () => box, esc: v => String(v ?? ""), actorName: a => String(a), fmtK: n => String(n),
  fmtElapsed: n => String(n), fmtSize: n => String(n), TERMINAL: new Set(),
  S: { state: null, think: new Map() },
  SessionObserver: { push: () => {}, usageFor: () => 5, toolName: n => String(n), resultSummary: () => "", cleanText: v => String(v ?? "") },
  AgentWorkbench: { push: () => {} },
  ToolParameters: { attach: () => {} },
  traceAppend: () => ({ dataset: {} }), traceScroll: () => {},
});
const core = fs.readFileSync(path.join(__dirname, "../src/spiritscout/web/static/js/core.js"), "utf8");
Object.assign(context, {atob, TextDecoder, Uint8Array});
vm.runInContext(core.slice(core.indexOf("function modelDisplayName("), core.indexOf("async function api(")), context);
vm.runInContext(cut("function traceTurn(", "function argBrief(") + cut("function tracePush(", 'const traceBox = $("#trace-box");'), context);

const push = (type, detail) => vm.runInContext("tracePush", context)({ type, actor: ACTOR, detail, ts: "2026-10-17T00:00:00Z" });
const say = () => children.filter(c => c.kind === "say");

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
console.log("trace stream markdown: model call boundaries finalize streamed text into rendered markdown passed");
