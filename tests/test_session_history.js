"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const historyPath=path.join(__dirname,'../src/spiritscout/web/static/js/session-history.js');
const historySource=fs.readFileSync(historyPath,'utf8');
const handlers={}, frames=new Map();let frameId=0,requests=[],rendered=[],observed=[],starts=0,stops=0,block=null;
let duringFrame=null;
const button={disabled:false},status={textContent:''};
const box={children:[],scrollTop:0,clientHeight:200,
  replaceChildren(){this.children=[];},
  get scrollHeight(){return this.children.length*100;},
  get lastElementChild(){return this.children.at(-1);},
  getBoundingClientRect(){return {top:0,bottom:200};},
  querySelectorAll(){return this.children;},
  addEventListener(name,fn){handlers[name]=fn;}};
const controls={querySelector(s){return s.includes('status')?status:button;}};
const ctx=vm.createContext({S:{cur:'one',view:'session',think:new Map()},AbortController,performance,
  requestAnimationFrame(fn){const id=++frameId;frames.set(id,fn);return id;},cancelAnimationFrame(id){frames.delete(id);},
  $:s=>s==='#trace-box'?box:s==='#trace-history'?controls:s==='#trace-jump'?{}:
    s==='#events'?{replaceChildren(){}}:undefined,
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
vm.runInContext(historySource.replace('return {open:', 'return {cachedPages:() => pages.filter(page => page.items).length, open:'),ctx);
const history=vm.runInContext('SessionHistory',ctx);
async function settle(){for(let i=0;i<30;i++){const pending=[...frames];frames.clear();pending.forEach(([,fn])=>{fn();duringFrame?.();});await new Promise(r=>setImmediate(r));}}
async function scroll(top){const previous=box.scrollTop;box.scrollTop=top;if(top===previous)handlers.wheel({deltaY:top===0?-1:1});else handlers.scroll();await settle();}
(async()=>{
  const opening=history.open();await settle();await opening;assert.equal(starts,1);assert.equal(rendered.length,4);
  await scroll(0);assert.equal(requests.length,2,'上滑自动请求历史');assert.equal(box.children.length,8);
  assert.equal(box.scrollTop,400,'插入历史后保持原消息位置');
  for(let i=0;i<20;i++){await scroll(0);assert.ok(history.cachedPages()<=12,'历史翻页的原文缓存不超过十二页');}
  assert.ok(box.children.length<=12,'只保留三段消息的 DOM');
  assert.equal(observed.length,4,'回看历史不重复更新实时统计');
  const count=requests.length,drawn=rendered.length;await scroll(box.scrollHeight-box.clientHeight);
  assert.equal(requests.length,count,'已读页面保留缓存，下滑回看不再重复请求');
  assert.ok(rendered.length>drawn,'窗口前滑仍重绘释放的较新内容');
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
  const forOldest=history.latest();await settle();await forOldest;
  handlers['document:click']({target:{closest:()=>({disabled:false,dataset:{history:'oldest'}})}});
  await settle();
  assert.ok(history.cachedPages()<=12,'定位开头的原文缓存也不超过十二页');
  assert.equal(box.children[0].dataset.historyId,'50','被裁剪的开头页可以重新读取');
  const forSync=history.latest();await settle();await forSync;
  block=async()=>({items:[{id:1100,event:{type:'report.ready',ts:'late-report'}}],cursor:1100,older_cursor:1000,has_more:true,activity_count:1});
  const sync=history.syncLatest();await settle();await sync;
  assert.equal(rendered.at(-1).ts,'late-report','终态轮询补收后台报告而不重绘历史');
  const afterSync=rendered.length;
  const syncAgain=history.syncLatest();await settle();await syncAgain;
  assert.equal(rendered.length,afterSync,'重复轮询按字节游标去重');
  block=null;
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

// 同一子任务的派发、不同推进阶段与结论有不同锚点，回看时不能跳到派发卡。
const identitySource=historySource
  .match(/function identity\(row\) \{[\s\S]*?\n  \}/)[0];
const rowIdentity=vm.runInContext(`(${identitySource})`,ctx);
const rowFor=extra=>({dataset:{agent:'web',taskId:'one',...extra}});
assert.notEqual(rowIdentity(rowFor({stepKey:'r:1'})),rowIdentity(rowFor({stepKey:'r:2'})));
assert.notEqual(rowIdentity(rowFor({taskPart:'dispatch'})),rowIdentity(rowFor({taskPart:'summary'})));
assert.equal(rowIdentity(rowFor({stepKey:'r:1',eventTs:'old'})),rowIdentity(rowFor({stepKey:'r:1',eventTs:'new'})));
// 「本页回合片段」行的身份不随窗口滑动（首行 id/时间戳换页即换）而变
const fragFor=extra=>({classList:{contains:c=>c==='tl-frag'},dataset:{agent:'web',...extra}});
assert.equal(rowIdentity(fragFor({eventTs:'a',historyId:'1'})),rowIdentity(fragFor({eventTs:'b',historyId:'2'})));
assert.notEqual(rowIdentity(fragFor()),rowIdentity({classList:{contains:()=>false},dataset:{agent:'web'}}));

// 新消息信封只按 detail.message_id 聚合：旧事件无 ID 时即使相邻也各自独立，
// 新 API 的附件和文字共享 ID 时合成一个 rail 存档点。
const messageKeySource=historySource.match(/  const messageKey = [\s\S]*?;\n  function anchorText/)[0]
  .replace(/\n  function anchorText[\s\S]*/,"");
const groupsSource=historySource.match(/  function groupsFromEvents\(items\) \{[\s\S]*?\n  \}\n  function indexForSession/)[0]
  .replace(/\n  function indexForSession[\s\S]*/,"");
const groupsFromEvents=vm.runInContext(`(()=>{${messageKeySource}${groupsSource};return groupsFromEvents;})()`,ctx);
const groups=groupsFromEvents([
  {id:10,event:{type:'user.message',detail:{content:'M1'}}},
  {id:20,event:{type:'user.attachment',detail:{name:'F2',message_id:'send-2'}}},
  {id:30,event:{type:'user.message',detail:{content:'M2',message_id:'send-2'}}},
  {id:40,event:{type:'user.attachment',detail:{name:'legacy-file'}}},
  {id:50,event:{type:'user.message',detail:{content:'legacy-text'}}},
]);
assert.equal(JSON.stringify(groups.map(group=>group.ids)),JSON.stringify([['10'],['20','30'],['40'],['50']]),
  'M1/F2/M2 只能形成 M1 与 F2+M2；旧无 ID 附件和文字不得按相邻时间猜测合并');
assert.equal(JSON.stringify(groups[1].parts),JSON.stringify(['[附件] F2','M2']));
console.log('session history anchors: message-id grouping and conservative legacy isolation passed');

const directionBox={scrollTop:83,scrollHeight:900,clientHeight:200},moves=[],pendingFrames=[];
const directionContext=vm.createContext({box:()=>directionBox,scrollTop:83,scrollDirection:0,scrollFrame:null,
  busy:false,ignoreScroll:false,past:true,S:{view:'session'},Math,
  requestAnimationFrame(fn){pendingFrames.push(fn);return 1;},markActiveAnchor(){},hideAnchorPreview(){},move:n=>moves.push(n)});
const onScroll=vm.runInContext(`(${historySource.match(/  function onScroll\(\) \{[\s\S]*?\n  \}/)[0]})`,directionContext);
directionBox.scrollTop=163;onScroll();pendingFrames.splice(0).forEach(fn=>fn());
assert.deepEqual(moves,[],'顶端区域向下80px不能反向加载旧页');
directionBox.scrollTop=100;onScroll();pendingFrames.splice(0).forEach(fn=>fn());
assert.deepEqual(moves,[-1],'向上接近顶端仍正常加载旧页');
const innerContext=vm.createContext({box:()=>directionBox,getComputedStyle:()=>({overflowY:'auto',overscrollBehaviorY:'auto'})});
const consumes=vm.runInContext(`(${historySource.match(/  function innerConsumesScroll\(target, delta\) \{[\s\S]*?\n  \}/)[0]})`,innerContext);
const output={nodeType:1,scrollTop:120,scrollHeight:1000,clientHeight:240,parentElement:directionBox};
assert.equal(consumes(output,-80),true,'内层可滚动时不能抢走滚轮事件');
output.scrollTop=0;assert.equal(consumes(output,-80),false,'内层边界允许滚动传给外层');

const markSource=historySource.match(/  function markActiveAnchor\(\) \{[\s\S]*?\n  \}/)[0];
const groupData=[{key:'first',ids:['10']},{key:'second',ids:['40','41']},{key:'file-only',ids:['80']}];
const bars=groupData.map(group=>({dataset:{anchorGroup:group.key},classList:{toggle(_,on){this.active=on;}}}));
const anchorBox={children:[],getBoundingClientRect:()=>({top:0})};
const markContext=vm.createContext({$:()=>({hidden:false,children:bars}),box:()=>anchorBox,indexForSession:()=>({groups:groupData}),visibleRect:()=>true});
const mark=vm.runInContext(`(${markSource})`,markContext);
const row=(id,top,user=false)=>({dataset:{historyId:String(id)},classList:{contains:name=>name==='tl-user'&&user},getBoundingClientRect:()=>({top,bottom:top+100})});
const selected=()=>bars.filter(bar=>bar.classList.active).map(bar=>bar.dataset.anchorGroup);
anchorBox.children=[row(60,-10)];mark();assert.deepEqual(selected(),['second'],'用户消息离开窗口后，回复仍归属原存档点');
anchorBox.children=[row(39,-80),row(40,49,true),row(41,106,true)];mark();assert.deepEqual(selected(),['first'],'未越过下一次用户消息不能提前跳点');
anchorBox.children=[row(39,-130),row(40,-1,true),row(41,56,true)];mark();assert.deepEqual(selected(),['second']);
anchorBox.children=[row(41,-10,true),row(42,50)];mark();assert.deepEqual(selected(),['second'],'同一发送的附件和正文是一个点');
anchorBox.children=[row(80,12.4,true)];mark();assert.deepEqual(selected(),['file-only'],'仅附件独立成点，容忍子像素舍入');
anchorBox.children=[row(39,-80),row(40,49,true)];mark();assert.deepEqual(selected(),['first'],'向上越过用户消息切回前一点');
console.log('archive point selection: offscreen ownership, message boundaries and attachment-only passed');
