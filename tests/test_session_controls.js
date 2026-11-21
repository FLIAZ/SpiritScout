"use strict";
const assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
const source = fs.readFileSync("src/spiritscout/web/static/js/views.js", "utf8");
const nodes = new Map(), calls = [];
function node(id) {
  if (!nodes.has(id)) nodes.set(id, {value:"", textContent:"", innerHTML:"", dataset:{}, disabled:false,
    classList:{toggle() {}}, setAttribute(name,value) {this[name]=value;}, querySelectorAll:()=>[], children:[]});
  return nodes.get(id);
}
const state = {cur:"sess-a", compFiles:[], state:{session:{session_id:"sess-a", title:"暂停测试", status:"running", mode:"ctf", paused:false}, recovery:{unresolved_count:3}}};
const ctx = vm.createContext(require('./helpers/i18n').context({S:state, $:node, TERMINAL:new Set(["completed","stopped","interrupted","failed"]),
  STATUS_CN:{},MODE_CN:{},esc:String,projectName:()=>"未分类",fmtDur:()=>"1 分钟",agentName:String,
  toast() {}, refreshState() {}, startRename() {}, openSession() {},
  ProjectsPage:{moveSession() {}},SessionStorage:{open() {}}, armDelete() {},
  api:async (url,opts)=>{calls.push({url,opts});return {...state.state.session,paused:url.endsWith("/pause")};},
}));
vm.runInContext(source.slice(source.indexOf("const sessionControlPending"),source.indexOf("/* Flag 概览")),ctx);
function testWorkspaceNavigation() {
  const html = fs.readFileSync('src/spiritscout/web/static/index.html', 'utf8');
  const navigation = fs.readFileSync('src/spiritscout/web/static/js/session-navigation.js', 'utf8');
  const panels = new Map(), calls = [];
  const doc = {activeElement: null};
  function element(dataset = {}, parent = null) {
    const classes = new Set();
    return {dataset, parentElement: parent, children: [], hidden: false, disabled: false, textContent: '', tabIndex: -1,
      style: {}, handlers: {}, classList: {contains: c => classes.has(c), toggle(c, on) { on ? classes.add(c) : classes.delete(c); }},
      setAttribute(k, v) { this[k] = v; }, matches(selector) { return selector === ':popover-open' && this.open; },
      hidePopover() { this.open = false; this.emit('toggle', {newState: 'closed'}); },
      closest(selector) {
        if (selector === '#session-functions') return this.directory ? directory : null;
        for (let node = this; node; node = node.parentElement) if (selector === '[hidden]' && node.hidden) return node;
        return null;
      },
      focus() { doc.activeElement = this; }, click() { if (!this.disabled) { this.onclick?.(); this.emit('click'); } },
      scrollIntoView() {}, addEventListener(key, fn) { (this.handlers[key] ||= []).push(fn); },
      emit(key, event = {}) { for (const fn of this.handlers[key] || []) fn({...event, currentTarget: this}); },
      querySelectorAll(selector) {
        if (selector.includes('data-tab') || selector.includes('role="tab"')) return this.children;
        return [];
      }};
  }
  const primary = element(), secondary = element(), directory = element(), toggle = element();
  const areas = ['delivery', 'records'].map(name => element({sessionFamily: name}, secondary));
  const top = html.slice(html.indexOf('id="s-tabs"'), html.indexOf('id="session-secondary"'));
  for (const match of top.matchAll(/<button[^>]*data-tab="([^"]+)"[^>]*>/g)) primary.children.push(element({tab: match[1]}, primary));
  const sub = html.slice(html.indexOf('id="session-secondary"'), html.indexOf('id="tab-area"'));
  for (const match of sub.matchAll(/<button[^>]*(data-tab|data-workspace-home)="([^"]+)"[^>]*>/g)) {
    const name = match[2], family = ['files', 'deliverables', 'report', 'delivery'].includes(name) ? areas[0] : areas[1];
    const button = element(match[1] === 'data-tab' ? {tab: name} : {workspaceHome: name}, family);
    family.children.push(button);
  }
  const secondaryTabs = areas.flatMap(area => area.children);
  secondary.querySelectorAll = selector => selector.includes('data-session-family') || selector.includes('tablist') ? areas : secondaryTabs;
  const tabs = [...primary.children, ...secondaryTabs.filter(b => b.dataset.tab)];
  const entries = [];
  const menu = html.slice(html.indexOf('id="session-functions"'), html.indexOf('id="system-resource-panel"'));
  for (const match of menu.matchAll(/<button[^>]*data-session-page="([^"]+)"[^>]*>/g)) {
    const entry = element({sessionPage: match[1]}, directory); entry.directory = true;
    const children = {location: element(), description: element(), count: element()};
    children.description.textContent = 'description-' + match[1];
    entry.querySelector = selector => selector.includes('location') ? children.location : selector.includes('description') ? children.description : children.count;
    entries.push(entry);
  }
  directory.querySelectorAll = () => entries;
  directory.querySelector = selector => entries.find(entry => selector.includes('"' + entry.dataset.sessionPage + '"')).querySelector('[data-function-count]');
  primary.querySelectorAll = selector => selector.includes('data-session-count') ? [] : primary.children;
  const summaries = ['status', 'tasks', 'running'].map(name => element({sessionSummary: name}));
  for (const tab of tabs) panels.set('#tab-' + tab.dataset.tab, element());
  const ids = {'s-tabs': primary, 'session-secondary': secondary, 'session-functions': directory,
    'session-functions-toggle': toggle, 'session-functions-dot': element()};
  doc.getElementById = id => ids[id];
  doc.querySelectorAll = selector => selector.includes('data-session-page') ? entries : selector.includes('data-session-summary') ? summaries : tabs;
  doc.querySelector = selector => tabs.find(tab => selector.includes('"' + tab.dataset.tab + '"'));
  const state = {cur: 'one', tab: 'chat', state: null};
  const context = vm.createContext(require('./helpers/i18n').context({S: state, document: doc, window: {addEventListener() {}},
    $: id => panels.get(id), STATUS_CN: {running: '运行中'},
    SessionObserver: {render() {}}, AgentWorkbench: {render() {}},
    SessionArchive: {load() { calls.push('archive'); }}, SessionFiles: {refresh() { calls.push('files'); }},
    BackgroundTasks: {open() { calls.push('background'); }, close() {}},
    renderAssetLibrary() {}, renderOpportunities() {}, renderVulns() {}, renderFlags() {}, renderChallenges() {}, drawTopo() {},
    loadReport() { calls.push('report'); }, TrafficPage: {sessionTab() {}},
  }));
  vm.runInContext(navigation, context);
  vm.runInContext(source.slice(0, source.indexOf('let stateRequestVersion')), context);
  const nav = vm.runInContext('SessionNavigation', context);
  const visible = () => primary.children.filter(b => !b.hidden).map(b => b.dataset.tab);
  const tab = name => tabs.find(b => b.dataset.tab === name);
  const jump = name => entries.find(b => b.dataset.sessionPage === name).click();
  const reset = (mode, id = 'one', side_session = null) => {
    state.cur = id; state.tab = 'chat';
    state.state = {session: {session_id: id, mode, status: 'running', side_session}, tasks: []};
    nav.reset(id, state.state.session); nav.update(state.state);
  };
  const expectations = {
    src: ['chat', 'topo', 'assets', 'vulns', 'delivery', 'records'],
    ctf: ['chat', 'topo', 'flags', 'delivery', 'records'],
    competition: ['chat', 'topo', 'challenges', 'flags', 'delivery', 'records'],
    awdp: ['chat', 'topo', 'vulns', 'delivery', 'records'],
  };
  for (const [mode, expected] of Object.entries(expectations)) {
    reset(mode); assert.deepEqual(visible(), expected, mode);
    tab('topo').click(); assert.equal(state.tab, 'topo', '所有模式探索链路一层直达');
    assert.equal(panels.get('#tab-topo').hidden, false);
  }
  reset('ctf'); jump('assets');
  assert.equal(state.tab, 'assets', 'CTF 仍可访问资产，不限制探索能力');
  assert.equal(doc.activeElement, tab('assets'), '目录跳转后焦点转交当前入口');
  assert.deepEqual(visible(), ['chat', 'topo', 'assets', 'flags', 'delivery', 'records']);
  nav.update({...state.state, vulns: {total: 2}, flags: {total: 1}});
  assert.equal(state.tab, 'assets'); assert.equal(tab('vulns').hidden, true, '新结果不自动重排或切页');
  assert.equal(ids['session-functions-dot'].hidden, false);
  assert.match(toggle['aria-label'], /漏洞 2/);
  jump('vulns'); assert.equal(state.tab, 'vulns');
  assert.equal(tab('assets').hidden, true, '离开后收起临时入口');
  tab('delivery').click(); assert.equal(state.tab, 'delivery');
  assert.equal(calls.length, 0, '总览不额外读取档案、报告或后台任务');
  jump('report'); assert.equal(state.tab, 'report'); assert.deepEqual(calls, ['report']);
  assert.equal(tab('delivery')['aria-selected'], 'true', '报告正文仍标识统一文件入口');
  assert.equal(tab('report')['aria-selected'], 'true');
  assert.equal(panels.get('#tab-report').hidden, false);
  assert.equal(secondary.hidden, false); assert.equal(areas[0].hidden, false);
  tab('records').click(); assert.equal(state.tab, 'records');
  jump('background'); assert.deepEqual(calls, ['report', 'background']);
  assert.equal(tab('records')['aria-controls'], 'tab-background');
  tab('events').focus(); areas[1].emit('keydown', {key: 'ArrowRight', preventDefault() {}});
  assert.equal(state.tab, 'traffic'); assert.equal(doc.activeElement, tab('traffic'));
  tab('chat').focus(); primary.emit('keydown', {key: 'ArrowRight', preventDefault() {}});
  assert.equal(state.tab, 'topo');
  jump('experience'); assert.equal(state.tab, 'experience', '经验保留独立入口');
  reset('src');
  assert.equal(tab('report').hidden, true); assert.equal(nav.available('report'), false);
  jump('report'); assert.equal(state.tab, 'chat', '不可用报告入口不能触发请求');
  assert.equal(nav.available('files'), true); jump('files'); assert.equal(state.tab, 'files');
  assert.equal(secondaryTabs.find(b => b.dataset.workspaceHome === 'delivery').hidden, false);
  reset('src', 'side', {parent_session_id: 'parent'});
  assert.equal(nav.available('files'), false, '旁路会话不伪造子 Agent 报告能力');
  reset('competition', 'two');
  const identity = tab('topo');
  nav.update({session: {session_id: 'one', mode: 'src'}, vulns: {total: 9}});
  assert.deepEqual(visible(), expectations.competition, '旧会话数据不能覆盖新模式');
  assert.equal(tab('topo'), identity, '轮询和模式更新不重建标签节点');
  assert.equal(ids['session-functions-dot'].hidden, true, '切换会话清理上次结果提示');
  reset('unknown'); assert.deepEqual(visible(), ['chat', 'topo', 'delivery', 'records']);
}

testWorkspaceNavigation();
(async()=>{
  ctx.renderSessionHead();
  assert.equal(node("#comp-send").dataset.action,"pause");
  assert.match(node("#comp-send").innerHTML,/<rect/);
  assert.equal(node("#comp-send")["aria-label"],"暂停任务");
  assert.doesNotMatch(node("#s-ops").innerHTML,/id="op-pause"/,"页头不重复展示任务控制按钮");
  assert.match(node("#s-ops").innerHTML,/更多操作/);
  for (const mode of ["ctf", "src", "competition"]) {
    state.state.session.mode=mode;
    ctx.renderSessionHead();
    assert.equal(node('#s-tabs [data-tab="report"]').hidden, mode==="src");
    assert.equal(node("#s-ops").innerHTML.includes("/report/download"), mode!=="src");
    assert.equal(node("#s-err").innerHTML,"",`${mode} 不展示未决操作提示`);
    state.state.session.error="请求失败，请重试";
    ctx.renderSessionHead();
    assert.equal(node("#s-err").innerHTML,'<div class="err-banner">请求失败，请重试</div>',"实际错误仍正常展示");
    delete state.state.session.error;
    ctx.renderSessionHead();
    assert.equal(node("#s-err").innerHTML,"","错误消除后清理提示区域");
  }
  assert.equal(state.state.recovery.unresolved_count,3,"移除展示不改变恢复记录");
  await ctx.toggleTaskPause();
  assert.equal(state.state.session.paused,true);
  assert.equal(node("#comp-send").dataset.action,"resume");
  assert.match(node("#comp-send").innerHTML,/<path/);
  await ctx.toggleTaskPause();
  assert.equal(state.state.session.paused,false);
  assert.deepEqual(calls.map(c=>c.url),["/api/sessions/sess-a/pause","/api/sessions/sess-a/resume"]);
  assert.equal(calls.some(c=>c.url.endsWith("/messages")),false,"暂停继续不重发任务或创建新轮次");
  node("#comp-input").value="补充消息";
  ctx.syncComposerControl();
  assert.equal(node("#comp-send").dataset.action,"send");
  assert.equal(node("#comp-send").disabled,false);
  node("#comp-input").value="";
  state.compFiles=[{name:"fixture.txt"}];ctx.syncComposerControl();
  assert.equal(node("#comp-send").dataset.action,"send","附件同样作为草稿");
  state.compFiles=[];
  state.state.session.status="completed";ctx.renderSessionHead();
  assert.equal(node("#comp-send").dataset.action,"send");
  assert.equal(node("#comp-send").disabled,true);
  assert.doesNotMatch(node("#comp-input").placeholder,/完整上下文/);

  // 切换会话后迟到的暂停响应不能覆盖新会话状态。
  state.state.session.status="running";
  let resolve;
  ctx.api=()=>new Promise(r=>{resolve=r;});
  const task=ctx.toggleTaskPause();
  assert.equal(node("#comp-send").disabled,true);
  await ctx.toggleTaskPause();
  state.cur="sess-b";
  state.state.session={session_id:"sess-b",status:"running",mode:"ctf",paused:false};
  resolve({session_id:"sess-a",paused:true});await task;
  assert.equal(state.state.session.session_id,"sess-b");assert.equal(state.state.session.paused,false);

  // 审批主文展示目的，函数名和参数摘要置于可展开的技术信息中。
  ctx.updateSessionActions=()=>{};
  state.state.pending_approvals=[{approval_id:"a",action:"mcp_yakit",purpose:"核对测试接口的访问控制，可能修改测试记录。",detail:"调用 mcp_yakit 参数摘要 digest"}];
  vm.runInContext(source.slice(source.indexOf("const APV_CN"),source.indexOf("function updateSessionActions")),ctx);
  ctx.renderApprovals();
  assert.match(node("#topo-ops-apv").innerHTML,/核对测试接口的访问控制/);
  assert.match(node("#topo-ops-apv").innerHTML,/查看工具与调用信息/);
  assert.match(node("#topo-ops-apv").innerHTML,/待确认：执行外部工具操作/);
  console.log("session controls: single pause/resume/send button, state isolation, recovery notice removal and readable approvals passed");
})().catch(e=>{console.error(e);process.exitCode=1;});

function testUserAnswerRendering() {
  const renderSource=source.slice(source.indexOf('function tracePush('),source.indexOf('const traceBox = $("#trace-box");'));
  const answer="只允许测试 A，不允许 B。\n".repeat(45)+"<完整末尾>";
  const e={type:"user.answered",actor:"web",detail:{question_id:"q-a",question:"是否允许测试 A 和 B？",answer}};
  for (const side_session of [false,true]) {
    const rows=[];
    const state={cur:"fixture",state:{session:{side_session}}};
    const renderCtx=vm.createContext(require('./helpers/i18n').context({
      S:state,traceRenderTarget:null,$:()=>({}),
      esc:s=>String(s).replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;"),
      traceAppend(event,html,cls){const row={html,cls,dataset:{}};rows.push(row);return row;},
    }));
    vm.runInContext(renderSource,renderCtx);
    // 同一持久事件用于实时渲染和刷新回放，不依赖客户端临时回显。
    renderCtx.tracePush(e,{observe:false,eventId:77});
    assert.equal(rows.length,1);assert.equal(rows[0].cls,"tl-user");
    assert.equal(rows[0].dataset.questionId,"q-a");
    assert.match(rows[0].html,/是否允许测试 A 和 B/);
    assert.match(rows[0].html,/&lt;完整末尾&gt;/);
    assert.equal((rows[0].html.match(/只允许测试 A/g)||[]).length,45);
    renderCtx.tracePush({type:"user.answered",actor:"system",detail:{question_id:"q-a",answer:"",reason:"停止"}},{observe:false});
    assert.equal(rows.length,1,"取消等待不能冒充用户答复");
  }
}
testUserAnswerRendering();
