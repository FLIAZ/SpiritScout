"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const historyPath=path.join(__dirname,'../src/spiritscout/web/static/js/session-history.js');
const historySource=fs.readFileSync(historyPath,'utf8');
const handlers={}, frames=new Map();let frameId=0,requests=[],rendered=[],observed=[],starts=0,stops=0,block=null;
let duringFrame=null, renderTarget=null;
const button={disabled:false},status={textContent:''};
const box={children:[],scrollTop:0,clientHeight:200,
  replaceChildren(){this.children=[];},
  get scrollHeight(){return this.children.length*100;},
  get lastElementChild(){return this.children.at(-1);},
  getBoundingClientRect(){return {top:0,bottom:200};},
  querySelectorAll(){return this.children;},
  addEventListener(name,fn){handlers[name]=fn;}};
const controls={querySelector(s){return s.includes('status')?status:button;}};
const ctx=vm.createContext(require('./helpers/i18n').context({S:{cur:'one',view:'session',think:new Map()},AbortController,performance,
  requestAnimationFrame(fn){const id=++frameId;frames.set(id,fn);return id;},cancelAnimationFrame(id){frames.delete(id);},
  $:s=>s==='#trace-box'?box:s==='#trace-history'?controls:s==='#trace-jump'?{}:
    s==='#events'?{replaceChildren(){}}:undefined,
  document:{addEventListener(name,fn){handlers['document:'+name]=fn;}},
  ToolParameters:{reset(){},prune(){}},SessionObserver:{push(e){observed.push(e);}},AgentWorkbench:{push(){}},
  traceReset(){(renderTarget || box).children=[];},traceStick:true,traceScroll(){},
  traceBeginRender(){renderTarget={children:[],get lastElementChild(){return this.children.at(-1);}};return renderTarget;},
  traceEndRender(stage,commit,mode){renderTarget=null;if(commit)box.children=mode==='prepend'?[...stage.children,...box.children]:stage.children;},
  traceStopFollowing(){ctx.traceStick=false;},
  stopStreams(){stops++;},startStreams(){starts++;},
  renderAuditEvent(event,opt){rendered.push(event);if(opt?.observe!==false)observed.push(event);
    const row={dataset:{eventTs:event.ts,agent:'user'},querySelectorAll(){return [];},
      getBoundingClientRect(){return {top:box.children.indexOf(row)*100-box.scrollTop};}};
    (renderTarget || box).children.push(row);},
  async api(url,options){requests.push(url);if(block)return block(url,options);
    const before=Number(new URL(url,'http://local').searchParams.get('before')||1000);
    return {items:[0,1,2,3].map(i=>({id:before-30+i*10,event:{type:'user.message',ts:String(before-30+i*10)}})),
      cursor:before,older_cursor:before-40,has_more:before>100,activity_count:4};},
}));
vm.runInContext(historySource.replace('return {open:', 'return {cachedPages:() => pages.filter(page => page.items).length, open:'),ctx);
const history=vm.runInContext('SessionHistory',ctx);
async function settle(){for(let i=0;i<30;i++){const pending=[...frames];frames.clear();pending.forEach(([,fn])=>{fn();duringFrame?.();});await new Promise(r=>setImmediate(r));}}
async function scroll(top){const previous=box.scrollTop;ctx.traceStick=false;box.scrollTop=top;if(top===previous)handlers.wheel({deltaY:top===0?-1:1});else handlers.scroll();await settle();}
(async()=>{
  const opening=history.open();await settle();await opening;assert.equal(starts,1);assert.equal(rendered.length,4);
  await scroll(0);assert.equal(requests.length,2);assert.equal(box.children.length,8);
  assert.equal(box.scrollTop,400,'向上补入历史时保留原消息位置');
  const retained=box.children.slice(), retainedText=retained.map(row=>row.dataset.historyId);
  for(let i=0;i<20;i++){await scroll(0);assert.ok(history.cachedPages()<=12,'原文缓存有界');}
  assert.ok(retained.every(row=>box.children.includes(row)),'读取更早数据不得裁掉已加载的消息');
  assert.deepEqual(retained.map(row=>row.dataset.historyId),retainedText);
  assert.equal(observed.length,4,'读取历史不重复计入实时统计');
  const count=requests.length,drawn=rendered.length;await scroll(box.scrollHeight-box.clientHeight);
  assert.equal(requests.length,count,'向下回看不重新读取审计');
  assert.equal(rendered.length,drawn,'虚拟列表滚动不重放消息');
  await scroll(100);
  const reading=box.children.slice(), readingTop=box.scrollTop;
  history.enqueue({type:'user.message',ts:'live'},1100,'one');await settle();
  assert.equal(observed.at(-1).ts,'live');assert.equal(rendered.at(-1).ts,'live');
  assert.ok(reading.every(row=>box.children.includes(row)),'回看期间的新消息只增量更新');
  assert.equal(box.scrollTop,readingTop);assert.ok(history.isPast());
  await history.latest();await settle();assert.ok(!history.isPast());
  assert.ok(reading.every(row=>box.children.includes(row)),'回到最新保留消息与交互状态');

  const fresh=history.open();await settle();await fresh;
  const saved=box.children;let failRead;
  block=()=>new Promise((_,reject)=>{failRead=reject;});
  const loadingHistory=scroll(0);await settle();const pendingRequests=requests.length;
  handlers.scroll();await settle();assert.equal(requests.length,pendingRequests);
  failRead(new Error('暂时离线'));await loadingHistory;await settle();
  assert.equal(box.children,saved);assert.match(status.textContent,/读取失败/);
  block=null;await scroll(0);assert.ok(!status.textContent.includes('读取失败'));

  duringFrame=()=>{
    if(!history.isRendering())return;
    duringFrame=null;history.enqueue({type:'user.message',ts:'arrived-during-prepend'},1200,'one');
  };
  await scroll(0);await settle();await history.drain();
  assert.equal(rendered.at(-1).ts,'arrived-during-prepend','加载历史期间的新消息排队到提交后，不得丢帧');
  assert.equal(ctx.S.evSeen,1200);
  handlers['document:click']({target:{closest:()=>({disabled:false,dataset:{history:'oldest'}})}});
  for(let i=0;i<10&&history.isRendering();i++)await settle();
  assert.ok(!history.isRendering());assert.ok(history.cachedPages()<=12);
  assert.equal(box.children[0].dataset.historyId,'50','定位开头补入缺失数据');
  const allRows=box.children.slice();await history.latest();await settle();
  assert.ok(allRows.every(row=>box.children.includes(row)));
  block=async()=>({items:[{id:1300,event:{type:'report.ready',ts:'late-report'}}],cursor:1300,older_cursor:1200,has_more:true,activity_count:1});
  const sync=history.syncLatest();await settle();await sync;
  assert.equal(rendered.at(-1).ts,'late-report');
  const afterSync=rendered.length;const again=history.syncLatest();await settle();await again;
  assert.equal(rendered.length,afterSync,'终态增量补收按字节游标去重');block=null;
  const beforeBurst=box.children.slice(),cost=rendered.length;
  for(let i=0;i<520;i++)history.enqueue({type:'user.message',ts:`burst-${i}`},2000+i,'one');
  await settle();await history.drain();
  assert.equal(rendered.at(-1).ts,'burst-519');assert.equal(rendered.length-cost,520,'持续输出每个事件只投影一次');
  assert.ok(beforeBurst.every(row=>box.children.includes(row)));assert.ok(history.cachedPages()<=12);
  const old=rendered.length;history.enqueue({type:'user.message',ts:'stale'},1200,'other');await settle();assert.equal(rendered.length,old);
  let release;block=()=>new Promise(resolve=>{release=resolve;});
  const loading=history.open();await new Promise(resolve=>setImmediate(resolve));
  history.cancel();ctx.S.cur='two';release({items:[],cursor:9,older_cursor:0,has_more:false});await loading;await settle();
  assert.equal(rendered.length,old,'切换会话后旧响应不得提交');
  console.log('session history: incremental prepend, stable messages, live queue, bounded raw cache and cancellation passed');
})().catch(e=>{console.error(e);process.exitCode=1;});

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
const answers=groupsFromEvents([
  {id:60,event:{type:"user.answered",detail:{question_id:"q",answer:"只允许 A，不允许 B"}}},
  {id:70,event:{type:"user.answered",detail:{question_id:"cancelled",answer:"",reason:"停止"}}},
]);
assert.equal(answers.length,1,"用户答复进入历史锚点，停止回收不伪造答复");
assert.equal(answers[0].parts[0],"只允许 A，不允许 B");
console.log('session history anchors: message-id grouping and conservative legacy isolation passed');

const directionBox={scrollTop:83,scrollHeight:900,clientHeight:200},moves=[],pendingFrames=[];
const directionContext=vm.createContext(require('./helpers/i18n').context({box:()=>directionBox,scrollTop:83,scrollDirection:0,scrollFrame:null,
  busy:false,ignoreScroll:false,past:true,traceStick:false,S:{view:'session'},Math,
  requestAnimationFrame(fn){pendingFrames.push(fn);return 1;},markActiveAnchor(){},hideAnchorPreview(){},move:n=>moves.push(n)}));
const onScroll=vm.runInContext(`(${historySource.match(/  function onScroll\(\) \{[\s\S]*?\n  \}/)[0]})`,directionContext);
directionBox.scrollTop=163;onScroll();pendingFrames.splice(0).forEach(fn=>fn());
assert.deepEqual(moves,[],'顶端区域向下80px不能反向加载旧页');
directionBox.scrollTop=100;onScroll();pendingFrames.splice(0).forEach(fn=>fn());
assert.deepEqual(moves,[-1],'向上接近顶端仍正常加载旧页');
directionContext.past=false;directionContext.traceStick=true;directionBox.scrollTop=700;
onScroll();pendingFrames.splice(0).forEach(fn=>fn());
assert.deepEqual(moves,[-1],'直播吸底的程序滚动不触发向后换窗');
assert.equal(directionContext.past,false,'内容增长后的程序滚动不切换为历史回看');
const innerContext=vm.createContext(require('./helpers/i18n').context({box:()=>directionBox,getComputedStyle:()=>({overflowY:'auto',overscrollBehaviorY:'auto'})}));
const consumes=vm.runInContext(`(${historySource.match(/  function innerConsumesScroll\(target, delta\) \{[\s\S]*?\n  \}/)[0]})`,innerContext);
const output={nodeType:1,scrollTop:120,scrollHeight:1000,clientHeight:240,parentElement:directionBox};
assert.equal(consumes(output,-80),true,'内层可滚动时不能抢走滚轮事件');
output.scrollTop=0;assert.equal(consumes(output,-80),false,'内层边界允许滚动传给外层');

const markSource=historySource.match(/  function markActiveAnchor\(\) \{[\s\S]*?\n  \}/)[0];
const groupData=[{key:'first',ids:['10']},{key:'second',ids:['40','41']},{key:'file-only',ids:['80']}];
const bars=groupData.map(group=>({dataset:{anchorGroup:group.key},classList:{toggle(_,on){this.active=on;}}}));
const anchorBox={children:[],getBoundingClientRect:()=>({top:0})};
const markContext=vm.createContext(require('./helpers/i18n').context({$:()=>({hidden:false,children:bars}),box:()=>anchorBox,viewport:()=>null,indexForSession:()=>({groups:groupData}),visibleRect:()=>true,selectedAnchor:null}));
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

// 切会话后首个 controls() 早于新记录清屏，旧 DOM 不得写入新目录。
const refreshAnchorsSource=historySource.match(/  function refreshAnchors\(\) \{[\s\S]*?\n  \}\n  function markActiveAnchor/)[0].replace(/\n  function markActiveAnchor[\s\S]*/,"");
const imported=[],rail={children:[],hidden:false,setAttribute(){},replaceChildren(...bars){this.children=bars;}};
const index={session:'new',events:new Map(),groups:[],revision:0,groupsRevision:0,loading:false,error:'',complete:true};
const anchorIsolationContext=vm.createContext(require('./helpers/i18n').context({$:()=>rail,indexForSession:()=>index,renderedSession:'old',anchorKey:null,busy:false,
  anchorRows:()=>[{dataset:{historyId:'1'},querySelector:()=>({textContent:'上一会话附件'})}],
  recordAnchor:item=>imported.push(item),anchorText:()=>"上一会话内容",previewBar:null,hideAnchorPreview(){},markActiveAnchor(){},loadAnchorIndex(){}}));
vm.runInContext(`(${refreshAnchorsSource})()`,anchorIsolationContext);
assert.equal(imported.length,0,'上一会话仍可见的 DOM 不能变成当前会话存档点');
assert.equal(rail.children.length,0);
console.log('session history: old DOM cannot seed another session archive index');

// 活动输出与浏览器滚动事件可跨帧到达；用户上滑应撤销先前排队的强制跟随。
const viewsSource=fs.readFileSync(path.join(__dirname,'../src/spiritscout/web/static/js/views.js'),'utf8');
const followFrames=[],followHandlers={},followJump={};
const followBox={_top:600,scrollHeight:1000,clientHeight:200,
  get scrollTop(){return this._top;},set scrollTop(value){this._top=Math.max(0,Math.min(value,this.scrollHeight-this.clientHeight));},
  addEventListener(name,fn){followHandlers[name]=fn;}};
const followContext=vm.createContext({window:{},traceStick:true,$:selector=>selector==='#trace-box'?followBox:followJump,
  SessionHistory:{isRendering:()=>false},requestAnimationFrame(fn){followFrames.push(fn);return followFrames.length;}});
vm.runInContext(viewsSource.slice(viewsSource.indexOf('let traceScrollFrame ='),viewsSource.indexOf('function traceTurn(')),followContext);
vm.runInContext(viewsSource.slice(viewsSource.indexOf('const traceBox = $("#trace-box");'),viewsSource.indexOf('$("#trace-jump").onclick')),followContext);
const follow=code=>vm.runInContext(code,followContext);
follow('traceScroll(true); traceStopFollowing();');followBox.scrollTop=480;
followFrames.splice(0).forEach(fn=>fn());
assert.equal(followBox.scrollTop,480,'上滑取消待执行的强制吸底，不被下一动画帧拉回');
follow('traceScroll(true)');followFrames.splice(0).forEach(fn=>fn());
assert.equal(followBox.scrollTop,800,'明确回到最新时正常吸底');
followBox.scrollHeight=1200;followHandlers.scroll();
assert.equal(followContext.traceStick,true,'吸底后新内容先到、scroll 后到，仍保持直播跟随');
follow('traceScroll()');followFrames.splice(0).forEach(fn=>fn());
assert.equal(followBox.scrollTop,1000);
followBox.scrollTop=700;followHandlers.scroll();
assert.equal(followContext.traceStick,false,'滚动条向上阅读也会退出自动跟随');
follow('traceScroll()');followFrames.splice(0).forEach(fn=>fn());
assert.equal(followBox.scrollTop,700,'后续实时刷新保留阅读位置');
console.log('live scrolling: queued follow cancellation, delayed scroll events and scrollbar reading passed');
