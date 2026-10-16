"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const context = vm.createContext({});
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
