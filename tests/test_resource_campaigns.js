"use strict";
// 资产分类筛选条：计数汇总、选态与点击行为（真实渲染函数，无浏览器依赖）。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../src/spiritscout/web/static/js/resources.js"), "utf8");
const renderer = source.slice(source.indexOf("function renderCampaigns(s)"), source.indexOf("function render(kind)"));
assert.ok(renderer.includes("renderCampaigns"), "找不到 renderCampaigns 渲染函数");

let chipEls = [], unnamedEl = null, loads = [];
const box = {
  hidden: false,
  innerHTML: "",
  querySelector: sel => sel === "[data-campaigns]" ? box
    : (sel === "[data-unnamed]" ? unnamedEl : null),
  querySelectorAll: sel => sel === "[data-campaign]" ? chipEls : [],
};
const context = vm.createContext({
  root: () => box,
  esc: value => String(value).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c])),
  load: kind => loads.push(kind),
});

const state = {
  campaign: "", unnamed: false, generation: 0,
  campaigns: [{name: "BiliBili SRC", count: 12}, {name: "某 CTF", count: 3}],
  unnamedCount: 4,
};
chipEls = [
  {dataset: {campaign: ""}},
  {dataset: {campaign: "BiliBili SRC"}},
  {dataset: {campaign: "某 CTF"}},
];
unnamedEl = {dataset: {}, onclick: null};

vm.runInContext(renderer, context);
context.__state = state;
vm.runInContext("renderCampaigns(__state)", context);

// 计数汇总与全部选中态
assert.match(box.innerHTML, /全部 19/, "全部 = 分类资产之和 + 未归类");
assert.match(box.innerHTML, /class="resource-chip on" data-campaign=""/, "默认选中「全部」");
assert.match(box.innerHTML, /BiliBili SRC 12/);
assert.match(box.innerHTML, /某 CTF 3/);
assert.match(box.innerHTML, /未归类 4/);
assert.equal(box.hidden, false);
assert.ok(chipEls.every(chip => typeof chip.onclick === "function"), "每个分类条可点击");
assert.equal(typeof unnamedEl.onclick, "function", "未归类条可点击");

// 点击分类条：切换过滤并重新加载；点击未归类：切换 unnamed 过滤
chipEls[1].onclick();
assert.equal(state.campaign, "BiliBili SRC");
assert.equal(state.unnamed, false);
assert.deepEqual(loads, ["assets"]);

state.campaign = "BiliBili SRC";
vm.runInContext("renderCampaigns(__state)", context);
assert.match(box.innerHTML, /class="resource-chip on" data-campaign="BiliBili SRC"/, "当前分类带选中态");
assert.doesNotMatch(box.innerHTML, /class="resource-chip on" data-campaign=""/, "「全部」不再选中");

unnamedEl.onclick();
assert.equal(state.unnamed, true);
assert.equal(state.campaign, "");
assert.deepEqual(loads, ["assets", "assets"]);
vm.runInContext("renderCampaigns(__state)", context);
assert.match(box.innerHTML, /class="resource-chip on" data-unnamed="1"/, "未归类带选中态");

// 无分类且无未归类：整个筛选条隐藏（新装环境不留空壳）
const empty = {campaign: "", unnamed: false, generation: 0, campaigns: [], unnamedCount: 0};
chipEls = [{dataset: {campaign: ""}}];
context.__state = empty;
vm.runInContext("renderCampaigns(__state)", context);
assert.equal(box.hidden, true);
assert.match(box.innerHTML, /全部 0/);

console.log("resource campaign chips: passed");
