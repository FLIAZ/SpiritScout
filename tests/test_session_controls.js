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
const ctx = vm.createContext({S:state, $:node, TERMINAL:new Set(["completed","stopped","interrupted","failed"]),
  STATUS_CN:{},MODE_CN:{},esc:String,projectName:()=>"未分类",fmtDur:()=>"1 分钟",agentName:String,
  toast() {}, refreshState() {}, startRename() {}, openSession() {},
  ProjectsPage:{moveSession() {}},SessionStorage:{open() {}}, armDelete() {},
  api:async (url,opts)=>{calls.push({url,opts});return {...state.state.session,paused:url.endsWith("/pause")};},
});
vm.runInContext(source.slice(source.indexOf("const sessionControlPending"),source.indexOf("/* Flag 概览")),ctx);
(async()=>{
  ctx.renderSessionHead();
  assert.equal(node("#comp-send").dataset.action,"pause");
  assert.match(node("#comp-send").innerHTML,/<rect/);
  assert.equal(node("#comp-send")["aria-label"],"暂停任务");
  assert.doesNotMatch(node("#s-ops").innerHTML,/id="op-pause"/,"页头不重复展示任务控制按钮");
  assert.match(node("#s-ops").innerHTML,/更多操作/);
  assert.doesNotMatch(node("#s-err").innerHTML,/尚待核对/,"CTF 不显示技术恢复提醒");
  state.state.session.mode="src";
  ctx.renderSessionHead();
  assert.match(node("#s-err").innerHTML,/尚待核对/,"SRC 保留未决操作提醒");
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
  console.log("session controls: single pause/resume/send button, state isolation, CTF notice and readable approvals passed");
})().catch(e=>{console.error(e);process.exitCode=1;});
