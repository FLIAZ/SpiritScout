"use strict";
/* 时间线回归：任务进展必须按事件发生顺序创建顶层续记卡；用户/主回复之后，
 * 即使仍是同一稳定阶段，也不能回写到回复之前的旧卡。工具结果继续按调用 ID 关联。 */
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm"),path=require("node:path");

class FakeEl {
  constructor(html){
    this.html=html||"";this.children=[];this.dataset={};this.isConnected=true;
    this.hidden=false;this.open=false;this.textContent="";this.innerHTML="";
    this.className=(String(html).match(/class="([^"]*)"/)||[])[1]||"";
    this.classList={contains:c=>(" "+this.className+" ").includes(" "+c+" "),
      toggle:c=>{const on=!this.classList.contains(c);this.className=on?`${this.className} ${c}`.trim():this.className.replace(c,"").trim();return on;},
      add:c=>{if(!this.classList.contains(c))this.className=`${this.className} ${c}`.trim();}};
    this._q=new Map();
  }
  get lastElementChild(){return this.children[this.children.length-1]||null;}
  insertAdjacentHTML(_,html){
    const el=new FakeEl(html);el._parent=this;
    el.remove=()=>{const i=this.children.indexOf(el);if(i>=0)this.children.splice(i,1);el.isConnected=false;};
    this.children.push(el);appended.push(el);return el;
  }
  querySelector(sel){if(!this._q.has(sel))this._q.set(sel,new FakeEl(""));return this._q.get(sel);}
}
const appended=[],topLevel=[];
const traceAppend=(event,html,cls="")=>{const row=new FakeEl(html);row.className=`tl ${cls}`;row.dataset.agent=event.actor;row.dataset.eventTs=event.ts;topLevel.push(row);return row;};
const S={tab:"chat",think:new Map()};
const ctx=vm.createContext({
  SessionObserver:{toolName:n=>n||"工具",cleanText:v=>String(v),resultSummary:d=>d.note||d.error||"返回结果"},
  traceAppend,traceScroll(){},mdRender:t=>String(t),esc:s=>String(s),actorName:a=>String(a||""),
  argBrief:()=>"",ToolParameters:{attach(){}},S,setTimeout,clearTimeout,
});
vm.runInContext(fs.readFileSync(path.join(__dirname,"../src/spiritscout/web/static/js/task-trace.js"),"utf8"),ctx);
const api=vm.runInContext("TaskTrace",ctx);
let seq=0;
const event=(type,detail={},actor="web-one")=>({type,detail,actor,seq:++seq,ts:String(seq)});
const push=(type,detail,actor)=>api.push(event(type,detail,actor));
const external=(type,label,actor)=>{const e=event(type,{},actor);assert.equal(api.push(e),false);traceAppend(e,label,type.startsWith("user.")?"tl-user":"tl-master");};

push("task.start",{task_id:"t1",title:"验证越权"});
push("agent.turn",{task_id:"t1",route_id:"r1",turn:1,angle:"确认注入点"});
push("agent.delta",{task_id:"t1",route_id:"r1",turn:1,kind:"text",text:"第一段进展"});
api.flush();
external("user.message","用户追问","user");
external("master.reply","灵枢答复","master");
// 同一个 r1/turn1 在对话后继续：必须新建顶层续记卡。
push("agent.delta",{task_id:"t1",route_id:"r1",turn:1,kind:"text",text:"回复后的新进展"});
push("agent.tool_call",{task_id:"t1",route_id:"r1",turn:1,calls:[{id:"c1",name:"shell",arguments:{command:"id"}}]});
api.flush();
external("user.message","第二次追问","user");
// 工具返回跨过用户消息：调用 ID 仍关联，但结果在用户消息后创建新续记卡。
push("agent.tool_result",{task_id:"t1",route_id:"r1",id:"c1",ok:true,result:"uid=0"});
push("agent.turn",{task_id:"t1",route_id:"r1",turn:2,angle:"验证后台"});
push("agent.progress",{task_id:"t1",route_id:"r1",turn:2,text:"第二阶段进展"});
push("task.done",{task_id:"t1",summary:{conclusion:"确认完成",routes_total:1,routes_done:1}});
api.flush();api.flush();

const kind=row=>row.html.includes("task-dispatch")?"dispatch":row.html.includes("task-continuation")?"continuation":
  row.html.includes("task-outcome")?"outcome":row.className.includes("tl-user")?"user":"master";
assert.deepEqual(topLevel.filter(row=>!row.hidden).map(kind),[
  "continuation","user","master","continuation","user","continuation","outcome",
],"只有有效正文续接时间线，跨消息工具返回不单独生成卡片");
assert.equal(topLevel.filter(row=>kind(row)==="dispatch").length,1,"派发信息保留在默认折叠记录中");
assert.equal(topLevel.filter(row=>kind(row)==="continuation").length,3,"仅意图正文创建消息块");
assert.equal(topLevel.filter(row=>kind(row)==="outcome").length,1,"真实结论只显示一次");
assert.ok(topLevel[0].hidden,"已有意图正文时不重复显示派发块");
const stageOf=row=>row.querySelector(".task-stage");
assert.equal(stageOf(topLevel[1]).querySelector(".task-stage-summary").innerHTML,"第一段进展");
assert.equal(stageOf(topLevel[4]).querySelector(".task-stage-summary").innerHTML,"回复后的新进展");
assert.equal(stageOf(topLevel[4]).querySelector(".task-step").querySelector(".task-progress").textContent,"本次执行已返回","迟到工具结果仍收在对应意图中");
assert.equal(appended.filter(el=>el.html.includes('class="tl-act"')).length,1,"同一工具的调用和返回只占一条折叠明细");
assert.equal(topLevel.at(-1).querySelector(".task-thread").querySelector(".task-summary").innerHTML,"确认完成");

api.reset();topLevel.length=0;appended.length=0;
push("task.start",{task_id:"src-done",title:"核对授权边界"},"recon-src");
push("agent.turn",{task_id:"src-done",route_id:"route-a",turn:1},"recon-src");
push("agent.delta",{task_id:"src-done",route_id:"route-a",turn:1,kind:"text",text:"A 路线完成现场核对。"},"recon-src");
push("agent.conclude",{task_id:"src-done",route_id:"route-a",turn:1,summary:"A 路线确认未发现越权。"},"recon-src");
push("task.done",{task_id:"src-done",summary:{routes_total:2,routes_done:2,status:"done"},conclusions:[
  {route:"route-a",text:"A 路线确认未发现越权。"},
  {route:"route-b",text:"B 路线确认存在未授权读取。"},
]},"recon-src");
api.flush();
const srcOutcome=topLevel.find(row=>!row.hidden && kind(row)==="outcome");
assert.ok(srcOutcome,"统计型 summary 之外仍有未展示结论时应生成结论卡");
assert.equal(srcOutcome.querySelector(".task-thread").querySelector(".task-summary").innerHTML,"B 路线确认存在未授权读取。");

api.reset();topLevel.length=0;appended.length=0;
push("task.start",{task_id:"intent",title:"分析权限"});
for(let i=1;i<=50;i++) {
  push("agent.turn",{task_id:"intent",route_id:"r",turn:i});
  push("model.started",{task_id:"intent",route_id:"r",turn:i,call_id:`model-${i}`});
  if(i===1 || i===50) push("agent.progress",{task_id:"intent",route_id:"r",turn:i,
    exploration:{intent:"核对权限边界",attempt:"对照成员权限"},text:i===1?"先确认成员能访问哪些数据。":"已找到一个跨成员访问入口。"});
  push("agent.tool_call",{task_id:"intent",route_id:"r",turn:i,calls:[{id:`cmd-${i}`,name:"shell",arguments:{command:"fixture"}}]});
  push("agent.tool_result",{task_id:"intent",route_id:"r",turn:i,id:`cmd-${i}`,ok:true,result:"fixture result"});
}
push("model.cancelled",{task_id:"intent",route_id:"r",turn:50,call_id:"model-50"});
api.flush();api.flush();
const intents=topLevel.filter(row=>!row.hidden && kind(row)==="continuation");
assert.equal(intents.length,1,"同一意图的50轮调用不生成50张卡");
const intent=stageOf(intents[0]);
assert.equal(intent.querySelector(".task-heading strong").textContent,"核对权限边界");
assert.match(intent.querySelector(".task-stage-summary").innerHTML,/先确认成员/);
assert.match(intent.querySelector(".task-stage-summary").innerHTML,/跨成员访问入口/);
assert.equal(intent.querySelector(".task-step").open,false,"执行明细默认折叠");
assert.match(intent.querySelector(".task-step").querySelector(".task-execution-records").textContent,/模型调用已取消/);
assert.equal(intent.querySelector(".task-status").textContent,"执行中","取消单次模型调用不冒充整项意图撤销");
assert.equal(appended.filter(el=>el.html.includes('class="tl-act"')).length,50,"实际工具记录仍可完整展开");

api.reset();topLevel.length=0;appended.length=0;
for(let i=1;i<=50;i++) {
  push("agent.turn",{task_id:"cancel-only",route_id:"r",turn:i});
  push("model.cancelled",{task_id:"cancel-only",route_id:"r",turn:i,call_id:`cancel-${i}`});
}
push("task.cancelled",{task_id:"cancel-only"});api.flush();
assert.equal(topLevel.length,0,"无意图正文的取消调用不生成空卡或暂无工具调用占位");
api.reset();topLevel.length=0;appended.length=0;
push("system.lifecycle",{action:"single_executor_start",mode:"ctf",agent_id:"ctf-misc-single"},"runtime");
push("executor.identity",{task_id:"single",kind:"single",user_direction_hints:["Reverse"]},"ctf-misc-single");
push("task.start",{task_id:"single",type:"ctf_single",title:"逆向附件"},"ctf-misc-single");
push("agent.turn",{task_id:"single",route_id:"r",turn:1,angle:"核对输入逻辑"},"ctf-misc-single");
push("agent.delta",{task_id:"single",route_id:"r",turn:1,kind:"text",text:"已确认文件为 PE。下一步核对输入逻辑。"},"ctf-misc-single");
api.flush();
assert.equal(api.actorLabel("ctf-misc-single"),"单执行者");
const singleStage=stageOf(topLevel.find(row=>!row.hidden && kind(row)==="continuation"));
assert.match(singleStage.querySelector(".task-context").textContent,/单执行者/);
assert.match(singleStage.querySelector(".task-context").textContent,/用户方向：Reverse（提示）/);
assert.match(singleStage.querySelector(".task-stage-summary").innerHTML,/已确认文件为 PE/);
api.reset();
assert.equal(api.actorLabel("ctf-misc-single"),"","身份不会跨会话泄漏到普通角色");
console.log("task trace render: intent-owned collapsed execution, sparse timeline and cancellation isolation passed");

api.reset();topLevel.length=0;appended.length=0;
api.push(event("agent.progress",{task_id:"range",route_id:"r",text:"已有进展",exploration:{intent:"同一意图"}}),100);
api.push(event("agent.progress",{task_id:"range",route_id:"r",text:"后续进展",exploration:{intent:"同一意图"}}),200);
api.flush();
let rangeRow=topLevel.find(row=>!row.hidden && kind(row)==="continuation");
assert.equal(rangeRow.dataset.historyStart,"100");assert.equal(rangeRow.dataset.historyEnd,"200");
external("user.message","用户补充","user");
api.push(event("agent.progress",{task_id:"range",route_id:"r",text:"新一轮进展",exploration:{intent:"同一意图"}}),300);
api.flush();
rangeRow=topLevel.filter(row=>!row.hidden && kind(row)==="continuation").at(-1);
assert.equal(rangeRow.dataset.historyStart,"300","跨用户消息的正文必须使用自己的事件游标");
assert.equal(rangeRow.dataset.historyId,"300");
api.reset();
console.log("task event ranges: stable loaded interval and user-message separation passed");
