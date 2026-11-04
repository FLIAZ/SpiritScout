"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const context = vm.createContext({});
const coreSource=fs.readFileSync(require("node:path").join(__dirname,"../src/spiritscout/web/static/js/core.js"),"utf8");
context.atob=atob;context.TextDecoder=TextDecoder;context.Uint8Array=Uint8Array;
vm.runInContext(coreSource.slice(coreSource.indexOf("function modelDisplayName("),coreSource.indexOf("async function api(")),context);
vm.runInContext(fs.readFileSync(require("node:path").join(__dirname,
  "../src/spiritscout/web/static/js/observer.js"), "utf8"), context);
const started = 1700000000;
context.state = {model_usage:{used_tokens:105796,calls:13,call_series:[
  {ts:started,model:"test-model",tokens:12110}]}, business_context:{evidence:[{url:"https://example.test"}]},
  tasks:[],session:{paused:true}};
context.history = [{type:"model.call",ts:new Date((started+240.6)*1000).toISOString(),
  detail:{tokens:"***REDACTED***",model:"test-model",latency_ms:240600}}];
assert.equal(vm.runInContext("SessionObserver.usageFor(history[0],state)",context),12110);
context.history[0].detail.model="different-model";
assert.equal(vm.runInContext("SessionObserver.usageFor(history[0],state)",context),null,
  "不匹配的历史用量不能凭空分配");
assert.equal(vm.runInContext("SessionObserver.derive(state,history).tokens",context),105796);
assert.equal(vm.runInContext("SessionObserver.derive(state,history).evidence.length",context),1);
context.result={ok:true,note:'{"code":-352,"message":"restricted"}'};
assert.match(vm.runInContext("SessionObserver.resultSummary(result)",context),/业务返回 -352/);
context.result={ok:true,note:'<!doctype html><title>Observed page</title>'};
assert.match(vm.runInContext("SessionObserver.resultSummary(result)",context),/返回网页 · Observed page/);
context.history=[{type:"model.started",detail:{call_id:"one"}}];
assert.equal(vm.runInContext("SessionObserver.derive(state,history).pending.length",context),1);
context.history.push({type:"model.cancelled",detail:{call_id:"one"}});
assert.equal(vm.runInContext("SessionObserver.derive(state,history).pending.length",context),0);
console.log("observer: historical usage correlation, missing metrics, evidence and business results passed");
context.result={ok:false,failure_summary:'目标元素位于可视区域外',note:'Timeout 5000ms'};
assert.equal(vm.runInContext('SessionObserver.resultSummary(result)',context),'目标元素位于可视区域外');
context.result={ok:false,note:'\x1b[2mTimeout 5000ms\x1b[22m'};
assert.equal(vm.runInContext('SessionObserver.resultSummary(result)',context).includes('\x1b'),false);
assert.equal(vm.runInContext("SessionObserver.toolName('skill_open')",context),'使用技能');
assert.equal(vm.runInContext("SessionObserver.toolName('skill_read')",context),'读取技能文件');
context.skillDetail={ok:true,name:'skill_open',note:'已载入技能「scope-reference」· SKILL.md'};
assert.match(vm.runInContext('SessionObserver.resultSummary(skillDetail)',context),/已载入技能「scope-reference」/);

const modelKey='@WyJkZWVwc2VlayIsImRlZXBzZWVrLWZsYXNoIl0';
context.state.model_usage.call_series=[{ts:started,model:modelKey,tokens:196136}];
context.history=[{type:'model.call',ts:new Date(started*1000).toISOString(),detail:{model:'deepseek-flash',model_key:modelKey}}];
assert.equal(vm.runInContext('SessionObserver.usageFor(history[0],state)',context),196136);
context.history[0].detail.model_key='another-provider';
assert.equal(vm.runInContext('SessionObserver.usageFor(history[0],state)',context),null);
const observerBox={dataset:{},innerHTML:'',querySelectorAll:()=>[],querySelector:()=>null};
context.$=()=>observerBox;context.document={querySelectorAll:()=>[]};
context.esc=String;context.actorName=String;context.agentName=String;
context.TERMINAL=new Set(['completed']);context.STATUS_CN={completed:'已完成'};context.TASK_CN={};
context.S={view:'session',tab:'chat',cur:'label-test',state:{session:{session_id:'label-test',status:'completed'},tasks:[],model_usage:{used_tokens:196136,calls:13,per_model_tokens:{[modelKey]:196136},cache:{groups:[{model:modelKey,actor:'gateway',cache_scope:'classification',hit_rate:0,total_input_tokens:717,cached_input_tokens:0,output_tokens:33,measured_calls:1}]}}}};
vm.runInContext('SessionObserver.render()',context);
assert.match(observerBox.innerHTML,/deepseek \/ deepseek-flash/);
assert.doesNotMatch(observerBox.innerHTML,/@WyJ/);
assert.match(observerBox.innerHTML,/196,136/);
console.log('observer: readable cached model identities and unchanged usage totals passed');

// 默认摘要不堆叠空数据；完整计量保留在原生折叠区。
assert.doesNotMatch(observerBox.innerHTML,/data-section="findings"|data-section="execution"/);
assert.doesNotMatch(observerBox.innerHTML,/业务分析尚未完成|等待执行进展|生成中的调用暂未计入/);
assert.match(observerBox.innerHTML,/<details data-section="usage-details" class="obs-details"><summary>用量详情/);
context.S.state.model_usage.cache={hit_rate:.935,priced_calls:21,estimated_cost_cny_min:.125,estimated_cost_cny_max:.125};
context.S.state.tasks=[{task_type:'ctf_web',status:'done',routes:[{note:'本题已完成。'},{note:'本题已完成。'}]}];
context.S.state.observed_evidence=[{url:'https://example.test/login',status_code:200}];
vm.runInContext("SessionObserver.push({type:'agent.tool_result',actor:'ctf_web',detail:{ok:true,note:'本题已完成。'}})",Object.assign(context,{requestAnimationFrame:()=>{}}));
vm.runInContext('SessionObserver.render()',context);
assert.equal((observerBox.innerHTML.match(/本题已完成。/g)||[]).length,1,'合并任务与近期事件中的重复进展');
assert.match(observerBox.innerHTML,/缓存命中 <b>93.5%/);
assert.match(observerBox.innerHTML,/费用估算 <b>¥0.1250/);
assert.match(observerBox.innerHTML,/<details class="obs-section obs-details" data-section="findings"><summary>发现与记录/);
assert.match(observerBox.innerHTML,/<details class="obs-section obs-details" data-section="execution"><summary>执行记录/);
assert.doesNotMatch(observerBox.innerHTML,/0 个任务在途|已知事实 · 0|待补信息 · 0/);
let restored=false;
const openedDetails={dataset:{section:'usage-details'},set open(value){restored=value;}};
observerBox.querySelectorAll=selector=>selector==='details[open]' ? [{dataset:{section:'usage-details'}}] : selector==='details' ? [openedDetails] : [];
context.S.state.model_usage.used_tokens++;
vm.runInContext('SessionObserver.render()',context);
assert.equal(restored,true,'实况刷新时保留用户展开的详情');
console.log('observer: compact summary, optional findings, deduplicated progress and expanded-state preservation passed');
