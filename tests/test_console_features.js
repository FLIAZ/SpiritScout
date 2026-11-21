"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const root = path.resolve(__dirname, "../src/spiritscout/web/static/js");
const escape = value => String(value ?? "").replace(/[&<>\"]/g, char => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[char]));
function load(file, name) {
  const context = {console, esc: escape, modelDisplayName: value => String(value)};
  return vm.runInNewContext(fs.readFileSync(path.join(root, file), "utf8") + `\n${name}`, require('./helpers/i18n').context(context));
}
function pickerFixture() {
  const rects = {comp: {left: 1040, right: 1200, top: 630, bottom: 664},
    home: {left: 600, right: 760, top: 120, bottom: 154}};
  const nodes = new Map(), listeners = {}, requests = [], state = {cur: "one", sessions: [], state: {session: {session_id: "one", model: ""}}};
  let respond;
  const doc = {activeElement: null, addEventListener(type, fn) { listeners[type] = fn; }};
  const win = {innerWidth: 1280, innerHeight: 720, addEventListener(type, fn) {listeners[type] = fn;}};
  function node(kind, menu = false) {
    const search = {value: "", focus() {doc.activeElement = this;}};
    const list = {innerHTML: "", querySelectorAll() {return [];}};
    const retry = {};
    return {hidden: menu, style: {}, innerHTML: "", setAttribute(k, v) {this[k] = v;},
      focus() {doc.activeElement = this;}, getBoundingClientRect() {return rects[kind];},
      parentElement: {getBoundingClientRect() {return rects[kind];}},
      get offsetHeight() {return Math.min(this.innerHTML.includes('listbox') ? (search.value ? 120 : 380) : this.innerHTML.includes('alert') ? 100 : 40, parseFloat(this.style.maxHeight) || Infinity);},
      querySelector(selector) {return selector === 'input' ? search : selector === '[role="listbox"]' ? list : selector === 'button' ? retry : {};},
      querySelectorAll() {return [];}};
  }
  for (const kind of ["comp", "home"]) {
    nodes.set(`#${kind}-model`, node(kind));nodes.set(`#${kind}-model-menu`, node(kind, true));
  }
  const context = require('./helpers/i18n').context({S: state, document: doc, window: win, $: key => nodes.get(key),
    esc: escape, modelDisplayName: String, toast() {}, api(url) {requests.push(url);return new Promise((resolve, reject) => {respond = {resolve, reject};});}});
  const ui = vm.runInNewContext(fs.readFileSync(path.join(root, "composer-model.js"), "utf8") + '\nComposerModels', context);
  ui.init();
  function bounds(kind) {
    const menu = nodes.get(`#${kind}-model-menu`), anchor = rects[kind];
    const left = anchor.left + parseFloat(menu.style.left), top = anchor.top + parseFloat(menu.style.top);
    return {left, top, right: left + parseFloat(menu.style.width), bottom: top + menu.offsetHeight};
  }
  return {ui, nodes, rects, win, listeners, requests, bounds, response: () => respond};
}
async function pickerPositioning() {
  const f = pickerFixture(), menu = f.nodes.get('#comp-model-menu');
  const loading = f.nodes.get('#comp-model').onclick();
  assert.equal(f.bounds('comp').right, f.rects.comp.right);
  assert.equal(f.bounds('comp').bottom, f.rects.comp.top - 8, '加载状态也锚定按钮上方');
  f.response().resolve({models: [{key: 'fixture', model: 'fixture'}]});await loading;
  assert.equal(f.bounds('comp').bottom, f.rects.comp.top - 8, '模型列表加载后重新测量高度');
  assert.ok(f.bounds('comp').top >= 16);
  menu.querySelector('input').value = 'fixture';menu.querySelector('input').oninput();
  assert.equal(f.bounds('comp').bottom, f.rects.comp.top - 8, '搜索缩短列表后仍贴近按钮');
  f.win.innerWidth = 800;f.win.innerHeight = 600;
  Object.assign(f.rects.comp, {left: 650, right: 810, top: 550, bottom: 584});f.listeners.resize();
  assert.equal(f.bounds('comp').right, 784, '调整窗口后候选框限制在视口内');
  assert.equal(f.bounds('comp').bottom, 542);
  const home = f.nodes.get('#home-model').onclick();
  f.response().resolve({models: [{model: 'fixture'}]});await home;
  assert.equal(menu.hidden, true, '切换输入区关闭原候选框');
  assert.equal(f.bounds('home').top, f.rects.home.bottom + 8, '按钮上方不足时向下展开');
  assert.ok(f.bounds('home').bottom <= f.win.innerHeight - 16);
  f.ui.close();
  const failed = f.nodes.get('#comp-model').onclick();f.response().reject(new Error('fixture'));await failed;
  assert.equal(f.bounds('comp').bottom, f.rects.comp.top - 8, '读取错误状态重新定位');
  f.listeners.keydown({key: 'Escape', preventDefault() {}});
  assert.equal(menu.hidden, true);
  assert.equal(f.nodes.get('#comp-model')['aria-expanded'], 'false');
  assert.ok(f.requests.every(url => url === '/api/models'), '定位、搜索与关闭不会发起模型调用或修改任务');
}
async function main() {
  await pickerPositioning();
  const models = load("composer-model.js", "ComposerModels");
  const rows = models.options({models: [
    {key: "alpha-key", model: "same", provider_ref: "alpha"},
    {key: "beta-key", model: "same", provider_ref: "beta"},
    {key: "disabled", model: "old", enabled: false},
  ]});
  assert.equal(rows.length, 3);
  assert.equal(rows[1].key, "alpha-key"); assert.equal(rows[2].key, "beta-key");
  let sid = "one", chosen = "alpha-key", resolve;
  const saved = [];
  const ctl = models.create({request: () => new Promise(done => { resolve = done; }),
    sessionId: () => sid, sessionModel: () => chosen,
    saveSession: (id, model) => { saved.push([id, model]); if (id === sid) chosen = model; }});
  await ctl.choose("home", "beta-key"); assert.equal(ctl.selected("home"), "beta-key");
  const changing = ctl.choose("comp", "beta-key"); assert.equal(ctl.pending("comp"), true);
  sid = "two"; resolve({model: "beta-key"}); await changing;
  assert.equal(chosen, "alpha-key"); assert.equal(saved[0][0], "one");
  const rejected = models.create({request: async () => { throw new Error("fixture failure"); },
    sessionId: () => "one", sessionModel: () => chosen, saveSession: () => assert.fail("failure must not save")});
  await assert.rejects(rejected.choose("comp", "bad")); assert.equal(rejected.pending("comp"), false);

  const notify = load("notify.js", "ConsoleNotify");
  const values = new Map(), raised = [], notices = [];
  const storage = {getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value)};
  const doc = {title: "fixture", visibilityState: "hidden"};
  const items = Array.from({length: 5}, (_, index) => ({id: String(index), title: "fixture", message: "fixture", session_id: "one"}));
  const notifications = notify.create({fetchItems: async () => ({items}), storage,
    notifyCtor: null, doc, win: {focus() {}}, toastFn: text => raised.push(text),
    isCurrent: () => false, openSession: id => notices.push(id)});
  await notifications.check(); assert.equal(raised.length, 4);
  await notifications.check(); assert.equal(raised.length, 4);
  notifications.clearFlash(); assert.equal(doc.title, "fixture");
  notifications.setEnabled(false); notifications.clearSeen();
  await notifications.check(); assert.equal(raised.length, 4);
  const blocked = notify.create({fetchItems: async () => ({items}),
    storage: {getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); }},
    notifyCtor: null, doc, win: {}, toastFn() {}, isCurrent: () => true, openSession() {}});
  doc.visibilityState = "visible"; await blocked.check();
  assert.equal(doc.title, "fixture");
  let permissions = 0, sent = 0;
  class Notification { static permission = "granted"; static async requestPermission() { permissions++; } constructor() { sent++; } }
  const system = notify.create({fetchItems: async () => ({items: [items[0]]}), storage: null,
    notifyCtor: Notification, doc: {title: "system", visibilityState: "hidden"}, win: {}, toastFn() {}, isCurrent: () => false, openSession() {}});
  await system.check(); assert.equal(sent, 1); assert.equal(permissions, 0);
  await system.requestPermission(); assert.equal(permissions, 1);

  const experience = load("session-experience.js", "SessionExperience");
  assert.match(experience.historyRows([{result: "duplicate", duplicates: 2}]), /与已有经验重复/);
  assert.match(experience.historyRows([{result: "unparsed", error: "<failure>"}]), /&lt;failure&gt;/);
  assert.match(experience.cards([{insight: "fixture", sources: [{ref: "topology:one@1", source_session: "sess-one"}]}]), /topology:one@1/);
  console.log("console features: model choices, stale responses, notification fallback/dedup, experience rendering passed");
}
main().catch(error => { console.error(error); process.exitCode = 1; });
