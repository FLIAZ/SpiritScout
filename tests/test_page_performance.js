"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const root = path.join(__dirname, "../src/spiritscout/web/static/js");
const core = fs.readFileSync(path.join(root, "core.js"), "utf8");
const consoleSource = fs.readFileSync(path.join(root, "console.js"), "utf8");

function sidebarLayout() {
  const source = fs.readFileSync(path.join(root, "sidebar.js"), "utf8");
  const setup = (saved = null, blocked = false, sizing = false) => {
    const nodes = new Map(), writes = [], events = {};
    const document = {activeElement: null};
    const node = id => {
      if (!nodes.has(id)) nodes.set(id, {dataset: {}, inert: false, attrs: {},
        label: {textContent: ""}, setAttribute(k, v) { this.attrs[k] = v; },
        querySelector() { return this.label; }, contains(target) { return target === this.child; },
        focus() { this.focused = true; document.activeElement = this; }});
      return nodes.get(id);
    };
    const media = {matches: false, addEventListener(_event, handler) { this.change = handler; }};
    const storage = {getItem() { if (blocked) throw Error("denied"); return saved; },
      setItem(key, value) { if (blocked) throw Error("denied"); writes.push([key, value]); }};
    const context = vm.createContext(require('./helpers/i18n').context({
      document: Object.assign(document, {documentElement: node("root"), getElementById: node, querySelector: () => null,
        addEventListener: (key, handler) => { events[key] = handler; }}),
      window: {localStorage: storage, matchMedia: () => media,
        addEventListener: (key, handler) => { events[key] = handler; }},
    }));
    let layout = null;
    if (sizing) {
      const nav = node("primary-nav"), frames = [], changes = [], observed = [];
      let density = 1, resized;
      const spaces = {"--nav-tail-space": 12, "--nav-group-space": 22, "--nav-item-space": 3,
        "--nav-label-space": 7, "--nav-separator-space": 0, "--nav-label-correction": 0};
      const child = label => ({visible: true, label, getClientRects() { return this.visible ? [{}] : []; },
        classList: {contains: name => label && name === "nav-section"}});
      nav.clientHeight = 620;
      nav.style = {setProperty(key, value) { changes.push([key, value]); density = Number(value); }};
      nav.children = [5, 5, 4].map(count => ({children: [child(true), ...Array.from({length: count}, () => child(false))],
        getClientRects: () => [{}], getBoundingClientRect() {
          const visible = this.children.filter(c => c.visible);
          return {height: visible.reduce((height, c) => height + (c.label ? 18 + 7 * density : 36), 0)
            + (visible.length - 1) * 3 * density};
        }}));
      context.getComputedStyle = element => element === nav
        ? {paddingTop: "0", paddingBottom: String(6 + 12 * density), rowGap: String(22 * density),
          getPropertyValue: key => String(spaces[key])}
        : element.label ? {paddingBottom: String(7 * density), marginBottom: "0"}
          : {rowGap: String(3 * density), paddingTop: "0"};
      context.ResizeObserver = class {
        constructor(callback) { resized = callback; }
        observe(element) { observed.push(element); }
      };
      context.requestAnimationFrame = callback => { frames.push(callback); return frames.length; };
      layout = {nav, frames, changes, observed, get density() { return density; },
        resize() { resized(); }, flush() { frames.shift()(); }};
    }
    vm.runInContext(source, context);
    return {node, writes, media, events, storage, document, layout, api: vm.runInContext("ConsoleSidebar", context)};
  };
  const adaptive = setup(null, false, true), fit = adaptive.layout;
  assert.equal(fit.observed.length, 4, "观察导航可用高度和分组内容，不轮询资源数据");
  fit.resize(); fit.resize();
  assert.equal(fit.frames.length, 1, "同一帧中的尺寸变化只测量一次");
  fit.flush();
  assert.ok(fit.density > 0 && fit.density < 1, "空间不足时按比例收紧留白");
  const compressed = fit.density;
  fit.resize(); fit.flush();
  assert.equal(fit.changes.length, 1, "留白更新后再次观察不能产生反馈循环");
  fit.nav.clientHeight = 720;
  fit.resize(); fit.flush();
  assert.equal(fit.density, 1, "空间恢复后回到宽松排布");
  fit.nav.clientHeight = 620;
  fit.resize(); fit.flush();
  assert.equal(fit.density, compressed, "变高再缩小不累积尺寸误差");
  fit.nav.children[2].children.at(-1).visible = false;
  fit.resize(); fit.flush();
  assert.ok(fit.density > compressed, "隐藏可选入口后按实际内容归还留白");
  fit.nav.clientHeight = 400;
  fit.resize(); fit.flush();
  assert.equal(fit.density, 0, "极矮窗口只耗尽留白，保留按钮大小与滚动兜底");
  assert.equal(adaptive.writes.length, 0, "自适应排布不覆盖用户侧栏偏好");
  const f = setup(), panel = f.node("session-column"), menu = f.node("sidebar-menu-toggle"), toggle = f.node("sidebar-sessions-toggle");
  assert.equal(panel.inert, false);
  assert.equal(f.writes.length, 0, "首次显示不覆盖偏好");
  menu.onclick();
  assert.equal(menu.attrs["aria-expanded"], "false");
  assert.equal(panel.inert, false, "收起标题不收起会话列表");
  panel.child = {};
  f.document.activeElement = panel.child;
  toggle.onclick();
  assert.equal(panel.inert, true, "收起即时移除目录的键盘和辅助技术交互");
  assert.equal(f.document.activeElement, toggle, "焦点先移回开关，不能留在已收起的目录");
  assert.equal(menu.attrs["aria-expanded"], "false", "收起会话不重置菜单宽度");
  for (let i = 0; i < 4; i++) toggle.onclick();
  assert.equal(panel.inert, true, "快速反复切换以后始终服从最后一次操作");
  assert.equal(f.node("session-column"), panel, "折叠不重建列表节点");
  const restored = setup(f.writes.at(-1)[1]);
  assert.equal(restored.node("session-column").inert, true);
  restored.node("sidebar-sessions-toggle").onclick();
  assert.equal(restored.node("session-column").inert, false);
  assert.equal(restored.node("sidebar-menu-toggle").attrs["aria-expanded"], "false");
  f.media.matches = true; f.media.change(); toggle.onclick();
  assert.equal(panel.inert, false, "窄屏可独立展开目录");
  const writes = f.writes.length;
  f.events.keydown({key: "Escape"});
  assert.equal(panel.inert, true);
  assert.equal(toggle.focused, true);
  assert.equal(f.writes.length, writes, "窄屏临时展开不覆盖桌面偏好");
  toggle.onclick(); f.api.closeOnMobile(); assert.equal(panel.inert, true);
  f.media.matches = false; f.media.change();
  assert.equal(panel.inert, true, "返回桌面恢复原来的折叠选择");
  f.events.storage({storageArea: f.storage, key: "ss-sidebar-layout", newValue: null});
  assert.equal(panel.inert, false);
  for (const value of ["invalid-json", '{"menuCollapsed":"false"}', "null"]) {
    assert.equal(setup(value).node("sidebar-menu-toggle").attrs["aria-expanded"], "true");
  }
  const contextual = setup(), directory = contextual.node("session-column");
  contextual.api.navigate("models");
  assert.equal(directory.inert, true, "配置页默认归还目录空间");
  contextual.node("sidebar-sessions-toggle").onclick();
  assert.equal(directory.inert, false, "配置页仍可按需查看会话");
  assert.equal(contextual.writes.length, 0, "临时展开不覆盖任务页偏好");
  contextual.api.navigate("traffic");
  assert.equal(directory.inert, true, "切换配置页清理临时展开");
  contextual.api.navigate("home");
  assert.equal(directory.inert, false, "返回任务页恢复目录");
  contextual.node("sidebar-sessions-toggle").onclick();
  contextual.api.navigate("models");
  contextual.node("sidebar-sessions-toggle").onclick();
  contextual.api.navigate("session");
  assert.equal(directory.inert, true, "会话阅读默认收起目录");
  assert.equal(contextual.node("sidebar-menu-toggle").attrs["aria-expanded"], "false", "正文阅读默认使用窄导航");
  contextual.node("sidebar-menu-toggle").onclick();
  contextual.node("sidebar-sessions-toggle").onclick();
  assert.equal(directory.inert, false, "阅读时可展开历史目录");
  contextual.api.navigate("home");
  assert.equal(directory.inert, true, "阅读时临时展开不改变首页折叠偏好");
  assert.equal(contextual.node("sidebar-menu-toggle").attrs["aria-expanded"], "true", "离开阅读恢复原有菜单偏好");
  assert.equal(contextual.node("session-column"), directory, "路由切换不重建目录");
  const denied = setup(null, true);
  denied.node("sidebar-menu-toggle").onclick(); denied.node("sidebar-sessions-toggle").onclick();
  assert.equal(denied.node("session-column").inert, true, "存储不可用时仍可切换布局");
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
  S.sessions = [{session_id: "fold-focus", project_id: "p", title: "会话", status: "running"}];
  storage.delete("ss-sess-folded");
  const body = {inert: false, contains: value => value === child}, child = {};
  const section = {dataset: {folder: "p"}, classList: {contains() { return !!this.folded; }, toggle(_name, value) { this.folded = value; }},
    querySelector: selector => selector === ".sess-folder-body" ? body : {textContent: "项目"}};
  const head = {closest: () => section, setAttribute() {}, focus() { context.document.activeElement = this; }};
  context.document = {activeElement: child};
  box.querySelectorAll = selector => selector === ".sess-folder-head" ? [head] : [];
  delete box.dataset.signature;
  render(); head.onclick();
  assert.equal(body.inert, true, "折叠项目时立即禁止目录内容交互");
  const afterFold = writes; render();
  assert.equal(writes, afterFold, "折叠后的同数据轮询不重建目录或中断动画");
  assert.equal(context.document.activeElement, head, "项目折叠将焦点从内容移回目录标题");
  S.sessions[0].title = "尚未刷新的新标题";
  head.onclick(); assert.equal(body.inert, false, "展开项目恢复目录交互");
  render(); assert.equal(writes, afterFold + 1, "折叠只能确认布局变化，不能吞掉尚未显示的新数据");

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
  assert.match(list.innerHTML, /data-account-start/, "空状态提供现有的添加入口");
  assert.match(list.innerHTML, /配置登录凭据/);
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
    syncAssetPickerVisibility() {},
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
