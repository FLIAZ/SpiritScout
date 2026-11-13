"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm"),path=require("node:path");
const source=fs.readFileSync(path.join(__dirname,"../src/spiritscout/web/static/js/views.js"),"utf8");
const root={innerHTML:"",_content:null};
const S={cur:"sess-one",state:{expert_scorecard:{calibration:["减少同质实验 <b>now</b>"]},testing_opportunities:{
  prioritized:[{opportunity_ref:"opportunity:one",kind:"investigation_lead",subject:"<script>bad</script>",
    state:"actionable",priority_score:82,recommended_work:"evidence_followup",stalled:true,
    direction_state:"diminishing",marginal_yield:.25,pivot_recommendation:"改用脚本与接口提取",
    blockers:[{description:"需要测试账号 <svg onload=x>",user_resolvable:true}],
    expected_information:"确认真实接口",decision_impact:"决定是否深入测试",next_minimal_experiment:"比较响应差异",
    stop_condition:"无独立证据时关闭",capability_candidates:[
      {tactic:"TA0001",capability:"web_boundary_test",basis:"已观测接口 <img src=x>",preferred:true,yield:{attempts:2,marginal_yield:.25}},
      {tactic:"SCANNER-CONTROL",capability:"targeted_template_validation",basis:"定向模板依据"}],
    priority_components:[{points:18,basis:"目标响应 <img src=x>"}]}],
  stalled:[{}],action_yield:{effective_action_rate:.5,empty_completion_count:1},
  portfolio_review:{target_concentration:{target:"https://a.test",share:.75,over_concentrated:true},
    low_yield_capabilities:[{capability:"web_boundary_test"}]}}}};
const esc=value=>String(value).replace(/[&<>"']/g,char=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[char]);
const context=vm.createContext(require("./helpers/i18n").context({S,esc,$:selector=>{
  assert.equal(selector,"#tab-opportunities");return root;
}}));
vm.runInContext(source.slice(source.indexOf("function renderOpportunities"),source.indexOf("function topoFocus")),context);
vm.runInContext("renderOpportunities()",context);
assert.match(root.innerHTML,/行动机会/);assert.match(root.innerHTML,/82/);assert.match(root.innerHTML,/50%/);
assert.match(root.innerHTML,/破壁 · Web 边界测试/);assert.match(root.innerHTML,/探微 · 定向扫描/);
assert.match(root.innerHTML,/边际收益下降/);assert.match(root.innerHTML,/25%/);assert.match(root.innerHTML,/建议换路/);
assert.match(root.innerHTML,/攻击面复盘/);assert.match(root.innerHTML,/需要测试账号 &lt;svg onload=x&gt;/);
assert.match(root.innerHTML,/专家校准/);assert.match(root.innerHTML,/减少同质实验 &lt;b&gt;now&lt;\/b&gt;/);
assert.match(root.innerHTML,/&lt;script&gt;bad&lt;\/script&gt;/);assert.ok(!root.innerHTML.includes("<script>bad</script>"));
assert.match(root.innerHTML,/&lt;img src=x&gt;/);assert.doesNotMatch(root.innerHTML,/\{\s*"opportunity_ref"/,
  "机会驾驶舱不应倾倒结构化 JSON");
const first=root.innerHTML;vm.runInContext("renderOpportunities()",context);assert.equal(root.innerHTML,first,
  "相同状态刷新不应重建机会卡");
S.cur="sess-two";S.state={testing_opportunities:{prioritized:[],stalled:[],action_yield:{}}};
vm.runInContext("renderOpportunities()",context);assert.match(root.innerHTML,/还没有形成可排序的行动机会/);
console.log("opportunities: ranked cards, metrics, escaping, compact output and session refresh passed");
