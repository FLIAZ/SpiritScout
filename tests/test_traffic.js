"use strict";
/* 无浏览器单测：切会话/离页的迟到响应不能覆盖新页面，详情只输出转义文本。 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
class Element {
  constructor() {
    this.nodes = new Map(); this.innerHTML = ""; this.textContent = ""; this.hidden = false;
    this.dataset = {}; this.attrs = {}; this.classes = new Set(); this.scrollTop = 0; this.scrollLeft = 0;
    this.classList = {toggle:(name, enabled) => enabled ? this.classes.add(name) : this.classes.delete(name)};
  }
  get innerHTML() { return this.html; }
  set innerHTML(value) {
    this.html = value; this.sessionButtons = null; this.projectButtons = null;
    this.nodes.delete(".traffic-table-wrap");
  }
  querySelector(selector) {
    if (selector === ".traffic-table-wrap" && !this.innerHTML.includes('class="traffic-table-wrap"')) return null;
    if (!this.nodes.has(selector)) this.nodes.set(selector, new Element());
    if (selector === ".traffic-table-wrap") this.nodes.get(selector).contains = node => this.querySelectorAll("[data-session]").includes(node);
    return this.nodes.get(selector);
  }
  querySelectorAll(selector) {
    if (selector === "[data-session]") {
      return this.sessionButtons ||= [...this.innerHTML.matchAll(/data-session="([^"]+)"/g)].map(match => {
        const button = new Element(), row = new Element();
        button.dataset.session = match[1]; button.closest = () => row; return button;
      });
    }
    if (selector === "button" && this.innerHTML.includes("data-project")) {
      return this.projectButtons ||= [...this.innerHTML.matchAll(/data-project="([^"]*)"/g)].map(match => {
        const button = new Element(); button.dataset.project = match[1]; return button;
      });
    }
    if (selector === "[data-traffic-row]") {
      if (this.nodes.has("[data-records]")) return this.nodes.get("[data-records]").querySelectorAll(selector);
      this.rowCache ||= new Map();
      return [...this.innerHTML.matchAll(/data-traffic-row="(\d+)"/g)].map(match => {
        if (!this.rowCache.has(match[1])) { const row = new Element(); row.dataset.trafficRow = match[1]; row.attrs = {}; row.setAttribute = (key,value) => row.attrs[key]=value; this.rowCache.set(match[1],row); }
        return this.rowCache.get(match[1]);
      });
    }
    if (selector !== "[data-record]") return [];
    this.buttons = [...this.innerHTML.matchAll(/data-record="(\d+)"/g)].map(match => {
      const element = new Element(); element.dataset.record = match[1]; return element;
    });
    return this.buttons;
  }
  replaceChildren() { this.innerHTML = ""; this.textContent = ""; }
  setAttribute(name, value) { this.attrs[name] = value; }
  focus(options) { context.document.activeElement = this; this.focusOptions = options; }
  scrollIntoView() {}
}
const roots = new Map(), pending = [], timers = new Map();
let timerId = 0;
const S = {view:"session", tab:"traffic", cur:"a"};
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const context = vm.createContext(require('./helpers/i18n').context({S, esc, URLSearchParams, document:{visibilityState:"visible"},
  $: selector => { if (!roots.has(selector)) roots.set(selector, new Element()); return roots.get(selector); },
  api: path => new Promise((resolve, reject) => pending.push({path, resolve, reject})),
  setTimeout: callback => { timers.set(++timerId, callback); return timerId; },
  clearTimeout: id => timers.delete(id)}));
vm.runInContext(fs.readFileSync(require("node:path").join(__dirname, "../src/spiritscout/web/static/js/traffic.js"), "utf8"), context);
const run = code => vm.runInContext(code, context);
const flush = async () => { for (let i = 0; i < 6; i++) await Promise.resolve(); };
const fireTimer = async () => { const [id, callback] = [...timers.entries()][0]; timers.delete(id); await callback(); };
const result = (id, url) => ({items:[{id, url, method:"GET", status:200, response_bytes:3, duration_ms:2}],
  summary:{total:1}, has_more:false});

(async () => {
  run("TrafficPage.sessionTab()");
  assert.match(pending[0].path, /sessions\/a\/traffic/);
  await fireTimer();
  assert.equal(pending.length, 1, "慢请求期间自动刷新不叠加请求");
  S.cur = "b"; run("TrafficPage.sessionTab()");
  assert.equal(timers.size, 1, "切会话只留一个刷新定时器");
  pending[1].resolve(result(2, "https://b.invalid/<script>")); await flush();
  const root = roots.get("#tab-traffic"), rows = root.querySelector("[data-records]");
  assert.match(rows.innerHTML, /b.invalid\//);
  assert.match(rows.innerHTML, /&lt;script&gt;/);
  pending[0].resolve(result(1, "https://a.invalid/")); await flush();
  assert.doesNotMatch(rows.innerHTML, /a.invalid/, "迟到的上一会话响应不覆盖当前列表");
  rows.buttons[0].onclick();
  assert.equal(rows.querySelectorAll("[data-traffic-row]")[0].attrs["aria-selected"], "true", "打开交互详情高亮当前记录");
  assert.match(pending[2].path, /sessions\/b\/traffic\/2$/);
  pending[2].resolve({id:2, url:"https://b.invalid", method:"GET", source:"http_probe", status:200,
    request_headers:[], response_headers:[], request_body:{text:"", encoding:"text"},
    response_body:{text:'<img src=x onerror="alert(1)">', encoding:"utf-8"}, response_complete:true});
  await flush();
  const detail = root.querySelector("[data-detail]");
  assert.match(detail.innerHTML, /&lt;img src=x onerror=&quot;/);
  assert.doesNotMatch(detail.innerHTML, /<img/);
  run("TrafficPage.sessionTab()");
  const late = pending[3];
  S.view = "home"; run('TrafficPage.navigate("home")');
  assert.equal(timers.size, 0, "离开页面停止刷新");
  late.resolve(result(3, "https://late.invalid")); await flush();
  assert.doesNotMatch(rows.innerHTML, /late.invalid/);
  S.view = "session"; S.tab = "traffic"; S.cur = "c"; run("TrafficPage.sessionTab()");
  pending[4].resolve({...result(55, "https://c.invalid/new"), has_more:true, next_before:6}); await flush();
  root.querySelector("[data-next]").onclick();
  assert.match(pending[5].path, /before=6/);
  pending[5].resolve(result(5, "https://c.invalid/old")); await flush();
  const count = pending.length;
  await fireTimer();
  assert.equal(pending.length, count, "浏览旧页时不被自动刷新打断");
  S.view = "home"; run('TrafficPage.navigate("home")');
  S.view = "session"; S.tab = "traffic"; S.cur = "d"; run("TrafficPage.sessionTab()");
  pending[6].resolve(result(6, "https://d.invalid/")); await flush();
  for (const name of ["query","method","body"]) root.querySelector(`[name="${name}"]`).value = "";
  root.querySelector('[name="status"]').value = "200";
  root.querySelector('[name="errors"]').checked = false;
  root.querySelector(".traffic-filter").onsubmit({preventDefault(){}});
  assert.match(pending[7].path,/status=200/);
  pending[7].resolve({...result(7,"https://d.invalid/matched"),filters:{status:"200"}}); await flush();
  assert.match(rows.innerHTML,/matched/);
  root.querySelector(".traffic-filter").onsubmit({preventDefault(){}});
  pending[8].resolve({...result(8,"https://d.invalid/ignored-filter"),items:[{id:8,status:404,url:"ignored-filter"}]}); await flush();
  assert.equal(rows.innerHTML,"","旧后端忽略筛选时，不显示未筛选的混合结果");
  assert.match(root.querySelector("[data-message]").textContent,/尚不支持/);
  S.view = "home"; run('TrafficPage.navigate("home")');

  const catalog = {
    projects:[{project_id:"*",name:"全部项目",total:2,session_count:2},{project_id:"p",name:"项目",total:2,session_count:2},{project_id:"",name:"未分类",total:0,session_count:0}],
    sessions:["first","second"].map(session_id => ({session_id,project_id:"p",title:session_id,total:1,errors:0,response_bytes:3,available:true})),
  };
  S.view = "traffic"; run('TrafficPage.navigate("traffic")');
  pending.at(-1).resolve(catalog); await flush();
  pending.at(-1).resolve(result(1,"https://first.invalid")); await flush();
  const global = roots.get("#view-traffic"), sessions = global.querySelector(".traffic-sessions");
  const viewport = sessions.querySelector(".traffic-table-wrap");
  viewport.scrollTop = 180; viewport.scrollLeft = 32;
  const second = sessions.querySelectorAll("[data-session]")[1];
  second.focus(); second.onclick();
  assert.equal(sessions.querySelector(".traffic-table-wrap"),viewport,"选择会话保留滚动容器");
  assert.equal(sessions.querySelectorAll("[data-session]")[1],second,"选择会话不替换按钮");
  assert.equal(second.attrs["aria-pressed"],"true");
  assert.equal(second.closest("tr").classes.has("traffic-selected"),true);
  assert.equal(sessions.querySelectorAll("[data-session]")[0].attrs["aria-pressed"],"false");
  assert.match(global.querySelector("[data-record-title]").textContent,/second/);
  assert.match(pending.at(-1).path,/sessions\/second\/traffic/);
  pending.at(-1).resolve(result(2,"https://second.invalid")); await flush();
  async function pollCatalog(data) {
    const polling = fireTimer(); await flush();
    assert.equal(pending.at(-1).path,"/api/traffic");
    pending.at(-1).resolve(data); await flush();
    pending.at(-1).resolve(result(2,"https://second.invalid")); await polling;
  }
  await pollCatalog(catalog);
  assert.equal(sessions.querySelector(".traffic-table-wrap"),viewport,"相同数据轮询不重建会话列表");
  assert.equal(viewport.scrollTop,180); assert.equal(viewport.scrollLeft,32);
  const updated = {...catalog,sessions:catalog.sessions.map(s=>({...s,total:9}))};
  await pollCatalog(updated);
  const refreshed = sessions.querySelector(".traffic-table-wrap"), focused = sessions.querySelectorAll("[data-session]")[1];
  assert.notEqual(refreshed,viewport,"统计更新渲染新数据");
  assert.equal(refreshed.scrollTop,180,"统计变化后保留垂直位置");
  assert.equal(refreshed.scrollLeft,32,"统计变化后保留水平位置");
  assert.equal(context.document.activeElement,focused,"统计变化后恢复会话按钮焦点");
  assert.equal(focused.focusOptions.preventScroll,true,"恢复焦点不触发滚动");
  assert.equal(focused.attrs["aria-pressed"],"true");
  global.querySelector(".traffic-projects").querySelectorAll("button")[1].onclick();
  assert.equal(sessions.querySelector(".traffic-table-wrap").scrollTop,0,"切换项目从新列表顶部开始");
  pending.at(-1).resolve(result(1,"https://first.invalid")); await flush();
  global.querySelector(".traffic-projects").querySelectorAll("button")[2].onclick();
  assert.equal(sessions.querySelector(".traffic-table-wrap"),null,"空项目没有残留滚动列表");
  S.view = "home"; run('TrafficPage.navigate("home")');
  console.log("traffic: stale responses, isolation, escaping, paging, filters and catalog scroll/focus passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
