"use strict";
// 使用真实渲染函数验证实时统计与乱序响应，不依赖浏览器或额外前端包。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../src/spiritscout/web/static/js/views.js"), "utf8");
const renderer = source.slice(source.indexOf("const fmtK ="), source.indexOf("function renderSessionHead()"));
const box = { innerHTML: "", querySelectorAll: () => [] };
const state = { view: "dash", sessions: [], cur: null, state: null };
const metrics = {
  total_tokens: 123, total_cost: 0.25, running: 0,
  daily: { "2026-10-15": 123 }, per_model: { "audit-model": 123 },
};
const context = vm.createContext({
  S: state, $: () => box, api: async () => metrics,
  esc: value => String(value), toast: () => {},
});
vm.runInContext(renderer, context);

(async () => {
  await vm.runInContext("renderDash()", context);
  assert.match(box.innerHTML, /audit-model/);
  assert.match(box.innerHTML, /2026-10-15/);
  assert.match(box.innerHTML, /\$0\.250/);

  const pending = [];
  context.api = () => new Promise(resolve => pending.push(resolve));
  const older = vm.runInContext("renderDash()", context);
  const newer = vm.runInContext("renderDash()", context);
  pending[1]({ ...metrics, per_model: { "latest-model": 456 } });
  await newer;
  const latest = box.innerHTML;
  pending[0]({ ...metrics, per_model: { "stale-model": 10 } });
  await older;
  assert.equal(box.innerHTML, latest, "迟到的旧响应不能覆盖新统计");
  assert.match(latest, /latest-model/);

  const abandoned = vm.runInContext("renderDash()", context);
  state.view = "home";
  pending[2](metrics);
  await abandoned;
  assert.equal(box.innerHTML, latest, "离开页面后不再绘制旧请求");
  console.log("dashboard live metrics and response ordering: passed");
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
