"use strict";
// 经验提炼页：空态诚实指引、经验卡按在册会话回链、提炼失败原样留痕。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "../src/spiritscout/web/static/js/experience.js"), "utf8");

// 最小 DOM 桩：同选择器返回同一子元素，innerHTML 写进桩里可断言
function fakeEl() {
  const children = new Map();
  return {
    dataset: {}, hidden: false, innerHTML: "", textContent: "",
    querySelector(sel) {
      if (!children.has(sel)) children.set(sel, fakeEl());
      return children.get(sel);
    },
    querySelectorAll() { return []; },
  };
}

const page = fakeEl();
const state = {fixture: null, opened: []};
const context = vm.createContext({
  $: sel => (sel === "#view-exp" ? page : fakeEl()),
  api: async () => state.fixture,
  esc: s => String(s ?? "").replace(/[&<>"]/g,
    c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c])),
  S: {sessions: [{session_id: "sess-live"}, {session_id: "sess-other"}]},
  openSession: id => state.opened.push(id),
  refreshSessions: () => {},
  URLSearchParams,
  setTimeout: () => 0, clearTimeout: () => {},
  console,
});

vm.runInContext(source, context);
const Page = vm.runInContext("ExperiencePage", context);
assert.equal(typeof Page.load, "function", "模块应导出 load");

const listEl = () => page.querySelector("[data-list]");
const histEl = () => page.querySelector("[data-hist]");
const moreEl = () => page.querySelector("[data-more]");

(async () => {
  // ① 空库：诚实说明经验从哪来、指向提炼记录，而不是一片空白
  state.fixture = {total: 0, items: [], has_more: false, tags: [],
                   sources: 0, last_ts: null, history: []};
  await Page.load();
  assert.match(listEl().innerHTML, /经验库还是空的/);
  assert.match(listEl().innerHTML, /提炼记录/);
  assert.equal(moreEl().hidden, true, "无更多数据时隐藏加载更多");

  // ② 有经验：在册会话回链可点，失联来源只展示不可点
  state.fixture = {
    total: 2, has_more: true, sources: 2, last_ts: 1760000000,
    tags: [{tag: "web", count: 2}, {tag: "越权", count: 1}],
    items: [
      {insight: "越权先比业务字段再下结论", context: "会员接口", tags: ["web", "越权"],
       source_session: "sess-live", source_title: "会员接口越权测试",
       source_model: "master", ts: 1760000000},
      {insight: "老会话的经验", context: "历史", tags: ["历史"],
       source_session: "sess-gone", source_title: "",
       source_model: "master", ts: 1759000000},
    ],
    history: [
      {session_id: "sess-gone", ts: "2026-10-17T10:00:00", result: "error",
       outcome: "failed", error: "ValueError: 模型输出为空"},
      {session_id: "sess-live", ts: "2026-10-17T09:00:00", result: "ok",
       outcome: "completed", insights: 2},
    ],
  };
  await Page.load();
  const cards = listEl().innerHTML;
  assert.match(cards, /data-open-session="sess-live"/);
  assert.doesNotMatch(cards, /data-open-session="sess-gone"/,
                      "失联会话不给点击回链");
  assert.match(cards, /会员接口越权测试/);
  assert.match(cards, /适用：会员接口/);
  // 提炼记录：失败徽标 + 原因原样呈现
  const hist = histEl().innerHTML;
  assert.match(hist, /失败/);
  assert.match(hist, /模型输出为空/);
  assert.match(hist, /data-open-session="sess-live"/);
  assert.doesNotMatch(hist, /data-open-session="sess-gone"/);
  assert.equal(moreEl().hidden, false, "还有下一页时展示加载更多");

  console.log("test_experience_page: 全部通过");
})().catch(err => { console.error(err); process.exit(1); });
