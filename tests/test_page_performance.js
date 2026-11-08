"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const root = path.join(__dirname, "../src/spiritscout/web/static/js");
const core = fs.readFileSync(path.join(root, "core.js"), "utf8");
const consoleSource = fs.readFileSync(path.join(root, "console.js"), "utf8");

function sidebarLayout() {
  const source = fs.readFileSync(path.join(root, "sidebar.js"), "utf8");
  const setup = (saved = null, blocked = false) => {
    const nodes = new Map(), writes = [], events = {};
    const node = id => {
      if (!nodes.has(id)) nodes.set(id, {dataset: {}, hidden: false, attrs: {},
        label: {textContent: ""}, setAttribute(k, v) { this.attrs[k] = v; },
        querySelector() { return this.label; }, contains() { return false; }, focus() { this.focused = true; }});
      return nodes.get(id);
    };
    const media = {matches: false, addEventListener(_event, handler) { this.change = handler; }};
    const storage = {getItem() { if (blocked) throw Error("denied"); return saved; },
      setItem(key, value) { if (blocked) throw Error("denied"); writes.push([key, value]); }};
    const context = vm.createContext(require('./helpers/i18n').context({
      document: {documentElement: node("root"), getElementById: node, querySelector: () => null,
        addEventListener: (key, handler) => { events[key] = handler; }},
      window: {localStorage: storage, matchMedia: () => media,
        addEventListener: (key, handler) => { events[key] = handler; }},
    }));
    vm.runInContext(source, context);
    return {node, writes, media, events, storage, api: vm.runInContext("ConsoleSidebar", context)};
  };
  const f = setup(), panel = f.node("session-column"), menu = f.node("sidebar-menu-toggle"), toggle = f.node("sidebar-sessions-toggle");
  assert.equal(panel.hidden, false);
  assert.equal(f.writes.length, 0, "首次显示不覆盖偏好");
  menu.onclick();
  assert.equal(menu.attrs["aria-expanded"], "false");
  assert.equal(panel.hidden, false, "收起标题不收起会话列表");
  toggle.onclick();
  assert.equal(panel.hidden, true);
  assert.equal(menu.attrs["aria-expanded"], "false", "收起会话不重置菜单宽度");
  const restored = setup(f.writes.at(-1)[1]);
  assert.equal(restored.node("session-column").hidden, true);
  restored.node("sidebar-sessions-toggle").onclick();
  assert.equal(restored.node("session-column").hidden, false);
  assert.equal(restored.node("sidebar-menu-toggle").attrs["aria-expanded"], "false");
  f.media.matches = true; f.media.change(); toggle.onclick();
  assert.equal(panel.hidden, false, "窄屏可独立展开目录");
  const writes = f.writes.length;
  f.events.keydown({key: "Escape"});
  assert.equal(panel.hidden, true);
  assert.equal(toggle.focused, true);
  assert.equal(f.writes.length, writes, "窄屏临时展开不覆盖桌面偏好");
  toggle.onclick(); f.api.closeOnMobile(); assert.equal(panel.hidden, true);
  f.media.matches = false; f.media.change();
  assert.equal(panel.hidden, true, "返回桌面恢复原来的折叠选择");
  f.events.storage({storageArea: f.storage, key: "ss-sidebar-layout", newValue: null});
  assert.equal(panel.hidden, false);
  for (const value of ["invalid-json", '{"menuCollapsed":"false"}', "null"]) {
    assert.equal(setup(value).node("sidebar-menu-toggle").attrs["aria-expanded"], "true");
  }
  const denied = setup(null, true);
  denied.node("sidebar-menu-toggle").onclick(); denied.node("sidebar-sessions-toggle").onclick();
  assert.equal(denied.node("session-column").hidden, true, "存储不可用时仍可切换布局");
}

function sidebar() {
  let writes = 0, editing = false, html = "";
  const storage = new Map();
  const box = {dataset: {}, querySelector: () => editing ? {} : null, querySelectorAll: () => [],
    set innerHTML(value) { html = value; writes++; }, get innerHTML() { return html; }};
  const S = {cur: null, sessions: [{session_id: "sess-one", title: "原标题", status: "running", target: "fixture", project_id: "p"}],
    projects: [{project_id: "p", name: "项目"}]};
  const context = vm.createContext(require('./helpers/i18n').context({S, $: () => box, esc: String, STATUS_CN: {},
    TERMINAL: new Set(["completed"]), localStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)}}));
  vm.runInContext(core.slice(core.indexOf("function sessionGroups"), core.indexOf("/* 重命名：")), context);
  const render = () => vm.runInContext("renderSessions()", context);
  render();
  for (let i = 0; i < 30; i++) {
    S.sessions = S.sessions.map(session => ({...session, model_usage: {tokens: i}}));
    render();
  }
  assert.equal(writes, 1, "用量更新与相同列表轮询不重建侧栏");
  S.projects[0].asset_count = 100;
  render(); assert.equal(writes, 1, "项目资产计数变化不影响侧栏");
  for (const change of [
    () => { S.sessions[0].title = "新标题"; },
    () => { S.sessions[0].status = "completed"; },
    () => { S.sessions[0].target = "new.invalid"; },
    () => { S.cur = "sess-one"; },
    () => { S.projects[0].name = "改名项目"; },
    () => { storage.set("ss-sess-folded", '["p"]'); },
    () => { S.sessions[0].project_id = "gone"; },
    () => { S.sessions.push({...S.sessions[0], session_id: "sess-two"}); },
    () => { S.sessions.reverse(); },
  ]) {
    const before = writes;
    change(); render(); assert.equal(writes, before + 1, "显示数据变化必须重新渲染");
    render(); assert.equal(writes, before + 1);
  }
  editing = true;
  S.sessions[0].title = "编辑期间的新标题";
  const before = writes;
  render(); assert.equal(writes, before, "重命名或删除确认期间保留当前 DOM");
  editing = false;
  render(); assert.equal(writes, before + 1, "编辑结束后补上延迟的更新");
  assert.ok(html.includes("编辑期间的新标题"));
  S.sessions = []; render();
  assert.match(html, /还没有会话/);
  const emptyWrites = writes;
  render(); assert.equal(writes, emptyWrites, "空列表也复用原有 DOM");
}

const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
};
async function browserAccountsOnDemand() {
  const calls = [], status = {}, list = {}, window = {};
  const page = {dataset: {ready: "yes"},
    querySelector: selector => selector === "[data-status]" ? status : list,
    querySelectorAll: () => []};
  const context = vm.createContext(require('./helpers/i18n').context({window,
    document: {querySelector: () => page},
    api: async url => { calls.push(url); return {items: []}; },
  }));
  vm.runInContext(fs.readFileSync(path.join(root, "browser-accounts.js"), "utf8"), context);
  const pageApi = vm.runInContext("BrowserAccountsPage", context);
  assert.equal(calls.length, 0, "首页启动不读取账号列表");
  await pageApi.navigate("home");
  assert.equal(calls.length, 0, "新建任务不读取账号列表");
  await pageApi.navigate("browser-accounts");
  assert.deepEqual(calls, ["/api/browser-accounts"], "打开账号管理页才读取公开摘要");
  assert.match(list.innerHTML, /添加第一个网站账号/);
  assert.equal(status.textContent, "");
  window.__SPIRITSCOUT_LIGHT__ = true;
  await pageApi.navigate("browser-accounts");
  assert.equal(calls.length, 1, "轻量模式不加载账号数据");
}
async function startup(navigate = false, reject = false) {
  const agents = deferred(), projects = deferred(), sessions = deferred();
  const calls = [], version = {textContent: ""}, opened = [];
  const S = {view: "home", cur: null, sessions: []};
  const context = vm.createContext(require('./helpers/i18n').context({S, window: {}, document: {querySelector: () => ({content: "fixture-version"})},
    $: () => version, TERMINAL: new Set(["completed"]),
    AgentSideChat: {init() {}}, AgentWorkbench: {init() {}},
    ComposerModels: {init: () => calls.push("models-ready")},
    ConsoleNotify: {init: () => calls.push("notify-ready"), check() {}},
    ConsolePolling: {create: () => ({start: () => calls.push("polling-ready")})},
    api: url => { calls.push(url); return agents.promise; },
    ProjectsPage: {loadCatalog: () => { calls.push("projects"); return projects.promise; }},
    refreshSessions: () => { calls.push("sessions"); return sessions.promise; }, refreshState() {},
    openSession: id => opened.push(id),
  }));
  const boot = vm.runInContext(consoleSource.slice(consoleSource.indexOf("(async function boot()")), context);
  assert.deepEqual(calls, ["models-ready", "notify-ready", "polling-ready", "/api/agents", "projects", "sessions"],
    "交互立即绑定，所有独立请求在任何响应到达前发出");
  assert.equal(version.textContent, "vfixture-version");
  if (navigate) S.view = "models";
  S.sessions = [{session_id: "sess-running", status: "running"}];
  sessions.resolve();
  await boot;
  assert.deepEqual(opened, navigate ? [] : ["sess-running"], "慢目录不阻塞进入会话，也不抢走用户已切换的页面");
  if (reject) { agents.reject(new Error("offline")); projects.reject(new Error("offline")); }
  else { agents.resolve({roster: []}); projects.resolve({projects: []}); }
  await Promise.resolve();
}

(async () => {
  sidebarLayout();
  sidebar();
  await browserAccountsOnDemand();
  await startup();
  await startup(true, true);
  console.log("page performance: independent sidebar toggles, list reuse, on-demand accounts and independent startup passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
