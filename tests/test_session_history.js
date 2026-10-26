"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const handlers={}, frames=new Map();let frameId=0,requests=[],rendered=[],observed=[],starts=0,stops=0,block=null;
let duringFrame=null;
const button={disabled:false},status={textContent:''};
const box={children:[],scrollTop:0,clientHeight:200,
  replaceChildren(){this.children=[];},
  get scrollHeight(){return this.children.length*100;},
  getBoundingClientRect(){return {top:0};},
  querySelectorAll(){return this.children;},
  addEventListener(name,fn){handlers[name]=fn;}};
const controls={querySelector(s){return s.includes('status')?status:button;}};
const ctx=vm.createContext({S:{cur:'one',view:'session',think:new Map()},AbortController,performance,
  requestAnimationFrame(fn){const id=++frameId;frames.set(id,fn);return id;},cancelAnimationFrame(id){frames.delete(id);},
  $:s=>s==='#trace-box'?box:s==='#trace-history'?controls:s==='#trace-jump'?{}:{replaceChildren(){}},
  document:{addEventListener(name,fn){handlers['document:'+name]=fn;}},
  ToolParameters:{reset(){},prune(){}},SessionObserver:{push(e){observed.push(e);}},AgentWorkbench:{push(){}},
  traceReset(){box.children=[];},traceStick:true,traceScroll(){},
  stopStreams(){stops++;},startStreams(){starts++;},
  renderAuditEvent(event,opt){rendered.push(event);if(opt?.observe!==false)observed.push(event);
    const row={dataset:{eventTs:event.ts,agent:'user'},querySelectorAll(){return [];},
      getBoundingClientRect(){return {top:box.children.indexOf(row)*100-box.scrollTop};}};
    box.children.push(row);},
  async api(url,options){requests.push(url);if(block)return block(url,options);
    const before=Number(new URL(url,'http://local').searchParams.get('before')||1000);
    return {items:[0,1,2,3].map(i=>({id:before-30+i*10,event:{type:'user.message',ts:String(before-30+i*10)}})),
      cursor:before,older_cursor:before-40,has_more:before>100,activity_count:4};},
});
vm.runInContext(fs.readFileSync('src/spiritscout/web/static/js/session-history.js','utf8'),ctx);
const history=vm.runInContext('SessionHistory',ctx);
async function settle(){for(let i=0;i<30;i++){const pending=[...frames];frames.clear();pending.forEach(([,fn])=>{fn();duringFrame?.();});await new Promise(r=>setImmediate(r));}}
async function scroll(top){box.scrollTop=top;handlers.scroll();await settle();}
(async()=>{
  const opening=history.open();await settle();await opening;assert.equal(starts,1);assert.equal(rendered.length,4);
  await scroll(0);assert.equal(requests.length,2,'上滑自动请求历史');assert.equal(box.children.length,8);
  assert.equal(box.scrollTop,400,'插入历史后保持原消息位置');
  for(let i=0;i<7;i++)await scroll(0);
  assert.ok(box.children.length<=12,'只保留三段消息的 DOM');
  assert.equal(observed.length,4,'回看历史不重复更新实时统计');
  const count=requests.length;await scroll(box.scrollHeight-box.clientHeight);
  assert.equal(requests.length,count+1,'下滑可重新读取释放的较新内容');
  history.enqueue({type:'user.message',ts:'live'},1100,'one');await settle();
  assert.equal(observed.at(-1).ts,'live','阅读历史时继续更新实时观察');
  assert.ok(!rendered.some(e=>e.ts==='live'),'实时事件不抢占历史阅读位置');
  const jumping=history.latest();await settle();await jumping;assert.ok(!history.isPast());
  history.enqueue({type:'user.message',ts:'new'},1101,'one');await settle();assert.equal(rendered.at(-1).ts,'new');
  // 失败不清空当前记录；重复滚动不能派出并行读取。
  const saved=box.children;
  let failRead;
  block=()=>new Promise((_,reject)=>{failRead=reject;});
  const loadingHistory=scroll(0);await settle();const pendingRequests=requests.length;
  handlers.scroll();await settle();assert.equal(requests.length,pendingRequests);
  failRead(new Error('暂时离线'));await loadingHistory;await settle();
  assert.equal(box.children,saved);assert.match(status.textContent,/读取失败/);
  block=null;await scroll(0);assert.ok(!status.textContent.includes('读取失败'));
  const jumpBack=history.latest();await settle();await jumpBack;
  // 持续实时输出也受同一窗口约束，最后一条不能在窗口轮换时丢失。
  duringFrame=()=>{
    if(!history.isRendering())return;
    duringFrame=null;
    history.enqueue({type:'user.message',ts:'arrived-during-rebuild'},3000,'one');
  };
  for(let i=0;i<520;i++)history.enqueue({type:'user.message',ts:`burst-${i}`},2000+i,'one');
  await settle();await history.drain();
  assert.ok(rendered.some(e=>e.ts==='burst-519'));
  assert.equal(rendered.at(-1).ts,'arrived-during-rebuild');assert.ok(box.children.length<=480);
  assert.equal(ctx.S.evSeen,3000);
  const old=rendered.length;history.enqueue({type:'user.message',ts:'stale'},1200,'other');await settle();assert.equal(rendered.length,old);
  let release;block=()=>new Promise(r=>{release=r;});const loading=history.latest();await new Promise(r=>setImmediate(r));
  history.cancel();ctx.S.cur='two';release({items:[],cursor:9,older_cursor:0,has_more:false});await loading;await settle();
  assert.equal(rendered.length,old,'切换会话后旧请求不能渲染');
  console.log('session history: automatic scroll, anchor, bounded DOM, reload, live isolation and cancellation passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
