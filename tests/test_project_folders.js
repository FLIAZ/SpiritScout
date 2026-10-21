"use strict";
// 项目文件夹卡片：项目/未分类/全部恒在、计数、选中态与只读标记。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "../src/spiritscout/web/static/js/projects.js"), "utf8");
const renderer = source.slice(
  source.indexOf("function projectCardsHtml"),
  source.indexOf("function statsHtml"));
assert.ok(renderer.includes("projectCardsHtml"), "找不到 projectCardsHtml 渲染函数");

const context = vm.createContext({
  ALL: "*",
  esc: value => String(value).replace(/[&<>"]/g, c =>
    ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c])),
});
vm.runInContext(renderer, context);
const render = (catalog, active) => {
  context.__catalog = catalog;
  context.__active = active;
  return vm.runInContext("projectCardsHtml(__catalog, __active)", context);
};

const catalog = {
  projects: [{
    project_id: "prj-1", name: "BiliBili <SRC>", description: "主站与直播",
    session_count: 2, asset_count: 5, finding_count: 1,
    last_seen: 1700000000,
  }],
  unclassified: {
    name: "未分类", description: "", read_only: true,
    session_count: 1, asset_count: 2, finding_count: 0, last_seen: null,
  },
  all: {
    name: "全部", description: "", read_only: true,
    session_count: 3, asset_count: 7, finding_count: 1,
    last_seen: 1700000000,
  },
};

const html = render(catalog, null);
assert.match(html, /BiliBili &lt;SRC&gt;/, "项目名经转义渲染");
assert.match(html, /2 会话 · 5 资产 · 1 发现/, "项目计数");
assert.match(html, /data-folder="prj-1"/);
assert.match(html, /data-folder=""/, "未分类恒在");
assert.match(html, /data-folder="\*"/, "全部恒在");
assert.match(html, /1 会话 · 2 资产 · 0 发现/);
assert.match(html, /3 会话 · 7 资产 · 1 发现/);
assert.match(html, /自动归档/, "未分类标注自动归档");
assert.match(html, /只读总览/, "全部标注只读总览");
assert.match(html, /未记录/, "无最近活动时显示未记录");
assert.doesNotMatch(html, /class="pfolder on"/, "总览视图无选中卡片");

const selected = render(catalog, "prj-1");
assert.equal((selected.match(/pfolder on/g) || []).length, 1, "仅一个选中卡片");
assert.match(selected, /class="pfolder on" data-folder="prj-1"/);
const unclassified = render(catalog, "");
assert.match(unclassified, /class="pfolder dashed on" data-folder=""/,
  "未分类选中态保留虚线样式");
const all = render(catalog, "*");
assert.match(all, /class="pfolder all on" data-folder="\*"/,
  "全部选中态保留总览样式");

// 空目录（尚无项目）仍应恒有未分类与全部
const empty = render({
  projects: [],
  unclassified: {name: "未分类", read_only: true},
  all: {name: "全部", read_only: true},
}, null);
assert.match(empty, /data-folder=""/);
assert.match(empty, /data-folder="\*"/);
assert.match(empty, /0 会话 · 0 资产 · 0 发现/);

console.log("project folders: passed");
