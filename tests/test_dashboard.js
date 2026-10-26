"use strict";
// 使用真实渲染函数验证实时统计与乱序响应，不依赖浏览器或额外前端包。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../src/spiritscout/web/static/js/views.js"), "utf8");
const renderer = source.slice(source.indexOf("const fmtK ="), source.indexOf("function renderSessionHead()"));
const nodes=new Map();
const node=selector=>{
  if(!nodes.has(selector)) nodes.set(selector,{innerHTML:"",textContent:"",hidden:false,dataset:{},querySelector:node,querySelectorAll:()=>[]});
  return nodes.get(selector);
};
const box = {addEventListener:()=>{},innerHTML:"",setAttribute:()=>{},querySelectorAll:()=>[],querySelector:selector=>selector === ".dash-console" ? (box.innerHTML ? {} : null) : node(selector)};
const html=()=>box.innerHTML+[...nodes.values()].map(n=>n.innerHTML || n.textContent || "").join("");
const state = { view: "dash", sessions: [], cur: null, state: null };
const metrics = {
  total_tokens: 123, total_cost: 0.25, running: 0,
  daily: { "2026-10-15": 123 }, per_model: { "audit-model": 123 },
};
const context = vm.createContext({
  S: state, $: () => box, api: async () => metrics,
  esc: value => String(value), toast: () => {}, MODE_CN:{}, STATUS_CN:{},
});
vm.runInContext(fs.readFileSync(path.join(__dirname,"../src/spiritscout/web/static/js/dashboard.js"),"utf8"),context);
vm.runInContext(renderer, context);

(async () => {
  await vm.runInContext("renderDash()", context);
  assert.match(html(), /audit-model/);
  assert.match(html(), /2026-10-15/);
  assert.match(html(), /\$0\.250/);

  const pending = [];
  context.api = () => new Promise(resolve => pending.push(resolve));
  const older = vm.runInContext("renderDash()", context);
  const newer = vm.runInContext("renderDash()", context);
  pending[1]({ ...metrics, per_model: { "latest-model": 456 } });
  await newer;
  const latest = html();
  pending[0]({ ...metrics, per_model: { "stale-model": 10 } });
  await older;
  assert.equal(html(), latest, "迟到的旧响应不能覆盖新统计");
  assert.match(latest, /latest-model/);

  const abandoned = vm.runInContext("renderDash()", context);
  state.view = "home";
  pending[2](metrics);
  await abandoned;
  assert.equal(html(), latest, "离开页面后不再绘制旧请求");
  assert.equal(vm.runInContext("Dashboard.clock(1792944000)",context).includes("2026"),true);
  assert.equal(vm.runInContext("Dashboard.percent(null)",context),"未提供");
  assert.equal(vm.runInContext("Dashboard.money({})",context),"未知");
  assert.match(vm.runInContext("Dashboard.clock(1792944000)",context),/00:00:00/);
  node("details").open=true;
  state.view="dash";
  context.api=async()=>metrics;
  await vm.runInContext("renderDash()",context);
  assert.equal(node("details").open,true,"统计刷新保留趋势明细展开状态");
  box.onchange({target:{dataset:{dashField:"sort"},value:"cost"}});
  context.sortRows=[
    {session_id:"low",selected:{cost:{cny_max:1}},recent_hour:{cost:{cny_max:99}}},
    {session_id:"high",selected:{cost:{cny_max:9}},recent_hour:{cost:{cny_max:0}}},
  ];
  assert.equal(vm.runInContext("Dashboard.filteredSessions(sortRows)[0].session_id",context),"high","费用排序遵循选定时间范围");
  context.chartRows=[
    {label:"10-26 10:00",calls:1,tokens:0,cost:{cny_min:0,cny_max:0},cache:{hit_rate:.95}},
    {label:"10-26 11:00",calls:1,tokens:10,cost:{cny_min:.02,cny_max:.04},cache:{hit_rate:.96}},
  ];
  const bars=vm.runInContext("Dashboard.trendChart(chartRows,'cost')",context);
  assert.doesNotMatch(bars,/class="dash-cache-path"/,"费用柱状图不再混入缓存曲线");
  assert.equal((bars.match(/class="dash-chart-bar"/g)||[]).length,1,"零值不绘制伪柱体");
  assert.match(bars,/>0\.050</,"小额费用根据实际数量级缩放");
  assert.equal((bars.match(/class="dash-chart-tick"/g)||[]).length,2,"每根柱下均有时间标签");
  assert.match(bars,/>10-26<\/tspan><tspan[^>]*>10:00<\/tspan>/,"日视图分两行显示日期与小时");
  context.dayRows=Array.from({length:24},(_,i)=>({...context.chartRows[1],label:`10-26 ${String(i).padStart(2,'0')}:00`}));
  const dayChart=vm.runInContext("Dashboard.trendChart(dayRows,'tokens')",context);
  assert.equal((dayChart.match(/class="dash-chart-tick"/g)||[]).length,24,"24 个小时标签不能抽样省略");
  assert.match(dayChart,/min-width:1736px/,"24 根柱子保留标签空间并在容器内滚动");
  const tokens=vm.runInContext("Dashboard.trendChart(chartRows,'tokens')",context);
  assert.doesNotMatch(tokens,/class="dash-cache-path"/,"Token 柱状图不混入缓存曲线");
  const cache=vm.runInContext("Dashboard.trendChart(chartRows,'cache')",context);
  assert.match(cache,/class="dash-cache-path"/,"缓存曲线仅出现在独立缓存视图");
  assert.doesNotMatch(cache,/class="dash-chart-bar"/);
  assert.equal((cache.match(/class="dash-cache-point"/g)||[]).length,2,"独立缓存点也能看见");
  assert.match(vm.runInContext("Dashboard.trendChart([{label:'empty',tokens:0,calls:0,cost:{},cache:{}}],'cost')",context),/暂无逐笔用量/);
  assert.match(bars,/data-dash-bucket="1"/,"柱体关联对应时间桶");
  assert.match(bars,/role="tooltip" hidden/,"即时提示不依赖浏览器原生 title");
  context.preciseBucket={label:"10-26 11:00",tokens:1234567,calls:3,cost:{cny_min:.02,cny_max:.04},cache:{hit_rate:.96}};
  const tip=vm.runInContext("Dashboard.tooltipContent(preciseBucket)",context);
  assert.match(tip,/1,234,567/,"悬浮提示显示完整 Token 数");
  assert.match(tip,/96.0%/);
  assert.match(tip,/北京时间/);
  const floating=node(".dash-chart-tooltip");
  Object.assign(floating,{hidden:true,offsetWidth:200,offsetHeight:90,style:{}});
  node(".dash-trend").getBoundingClientRect=()=>({left:100,top:100,width:500});
  const group={dataset:{dashBucket:"0"},contains:()=>false,querySelector:()=>({getBoundingClientRect:()=>({left:450,top:300,width:36})})};
  const hover={target:{closest:()=>group},relatedTarget:null};
  box.onpointerover(hover);
  assert.equal(floating.hidden,false,"移入柱体即时显示提示");
  assert.equal(floating.style.top,"100px","提示位于柱顶上方并留出间隔");
  assert.equal(floating.style.left,"268px","提示围绕柱体居中");
  box.onpointerout(hover);
  assert.equal(floating.hidden,true,"离开柱体隐藏提示");
  console.log("dashboard live metrics, Beijing clock, unknown values and response ordering: passed");
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
