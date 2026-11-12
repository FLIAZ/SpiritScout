"use strict";
const assert = require('node:assert/strict');
const fs = require('node:fs'), vm = require('node:vm'), path = require('node:path');
const context = vm.createContext(require('./helpers/i18n').context({}));
vm.runInContext(fs.readFileSync(path.join(__dirname, '../src/spiritscout/web/static/js/polling.js'), 'utf8'), context);
(async () => {
  let time=0, timer, list=0, state=0, visible=true, session=true;
  const scheduler=vm.runInContext('ConsolePolling',context).create({
    now:()=>time, later:fn=>{timer=fn;return 1;}, cancel:()=>{timer=null;},
    refreshList:()=>list++, refreshState:()=>state++, isVisible:()=>visible,
    isSession:()=>session, isRunning:()=>true});
  const advance=async seconds=>{for(let i=0;i<seconds;i++){time+=1000;timer?.();await Promise.resolve();}};
  scheduler.start(); await advance(15);
  assert.equal(list,3,'列表每 5 秒一次，不叠加会话轮询');
  assert.equal(state,5,'活动状态每 3 秒兜底');
  scheduler.stateRequested(); time+=2000;timer();await Promise.resolve();assert.equal(state,5);
  visible=false;await advance(10);assert.equal(list,3,'隐藏后降频');
  visible=true;scheduler.wake();await Promise.resolve();assert.equal(list,4);assert.equal(state,6);
  session=false;await advance(15);assert.equal(list,7);assert.equal(state,6,'离开会话后不轮询旧状态');
  scheduler.stop();await advance(40);assert.equal(list,7);
  // 两个来源同时刷新列表，只能有一个实际在途请求。
  const source=fs.readFileSync(path.join(__dirname,'../src/spiritscout/web/static/js/core.js'),'utf8');
  let requests=0,resolve;
  const sandbox=vm.createContext(require('./helpers/i18n').context({S:{sessions:[],cur:null,view:'home'}, $:()=>({remove(){}}),
    renderSessions(){}, api:()=>{requests++;return new Promise(r=>{resolve=r;});}}));
  vm.runInContext(source.slice(source.indexOf('let sessionsInFlight'),source.indexOf('function renderSessions()')),sandbox);
  const first=vm.runInContext('refreshSessions()',sandbox),second=vm.runInContext('refreshSessions()',sandbox);
  assert.equal(first,second);assert.equal(requests,1);resolve({sessions:[]});await first;
  const third=vm.runInContext('refreshSessions()',sandbox);assert.equal(requests,2);resolve({sessions:[]});await third;
  // 发现登记与公开进展在会话仍运行时立即触发状态刷新，不等待 model.call 或 done。
  const views=fs.readFileSync(path.join(__dirname,'../src/spiritscout/web/static/js/views.js'),'utf8');
  let refreshes=0,queued=0;const listeners={};
  const liveState={cur:'live',evSeen:0};
  const live=vm.createContext(require('./helpers/i18n').context({S:liveState,
    stopStreams(){},setInterval(){return 1;},scheduleStateRefresh(){refreshes++;},
    SessionHistory:{enqueue(){queued++;}},
    EventSource:class{addEventListener(name,handler){listeners[name]=handler;}}}));
  vm.runInContext(views.slice(views.indexOf('function startStreams('),views.indexOf('function stopStreams(')),live);
  vm.runInContext('startStreams(false)',live);
  for(const [index,type] of ['agent.discovery','agent.progress','agent.tool_result'].entries())
    listeners.audit({data:JSON.stringify({type,detail:{}}),lastEventId:String(index+1)});
  assert.equal(refreshes,3,'运行中的发现和观察必须及时刷新图');
  listeners.audit({data:JSON.stringify({type:'agent.delta'}),lastEventId:'4'});
  assert.equal(refreshes,3,'逐 token 片段不重复拉取状态');assert.equal(queued,4);
  listeners.audit({data:JSON.stringify({type:'agent.discovery'}),lastEventId:'3'});
  assert.equal(refreshes,3,'重连重复事件不能重复刷新');
  liveState.cur='different';
  listeners.audit({data:JSON.stringify({type:'agent.discovery'}),lastEventId:'5'});
  assert.equal(refreshes,3,'切换会话后不刷新旧会话');
  // 使用真实刷新函数验证：突发事件合并、在途只排一次补刷、轮询与 SSE 共用请求。
  let clockMs=10000,nextTimer=0;const timers=new Map(),stateRequests=[];
  const stateDoc={visibilityState:'visible'},stateS={cur:'A',tab:'topo',polling:{stateRequested(){}}};
  const noOp=()=>{};
  const refreshContext=vm.createContext(require('./helpers/i18n').context({S:stateS,document:stateDoc,
    Date:{now:()=>clockMs},setTimeout(fn,delay){const id=++nextTimer;timers.set(id,{fn,at:clockMs+delay});return id;},
    clearTimeout(id){timers.delete(id);},api:url=>new Promise(resolve=>stateRequests.push({url,resolve})),
    TERMINAL:new Set(),renderSessionHead:noOp,renderChips:noOp,renderApprovals:noOp,renderQuestions:noOp,
    drawTopo:noOp,renderChallenges:noOp,SessionObserver:{render:noOp},AgentWorkbench:{render:noOp},
    ToolParameters:{refresh:noOp},toast:noOp}));
  vm.runInContext(views.slice(views.indexOf('let stateRequestVersion'),views.indexOf('/* 完成标记')),refreshContext);
  const schedule=()=>vm.runInContext('scheduleStateRefresh()',refreshContext);
  const drain=async()=>{for(let i=0;i<5;i++)await Promise.resolve();};
  const move=async ms=>{
    clockMs+=ms;
    for(const [id,row] of [...timers])if(row.at<=clockMs){timers.delete(id);row.fn();}
    await drain();
  };
  for(let i=0;i<20;i++)schedule();
  assert.equal(timers.size,1);
  await move(199);assert.equal(stateRequests.length,0);
  await move(1);assert.equal(stateRequests.length,1);
  for(let i=0;i<20;i++)schedule();
  assert.equal(timers.size,0,'在途更新只置脏，不另外启动定时请求');
  stateRequests[0].resolve({session:{status:'running',id:'A'}});await drain();
  assert.equal(timers.size,1,'在途多个更新只合并为一次补刷');
  await move(749);assert.equal(stateRequests.length,1);
  await move(1);assert.equal(stateRequests.length,2,'持续事件受最小刷新间隔约束');
  stateRequests[1].resolve({session:{status:'running',id:'A'}});await drain();
  schedule();assert.equal(timers.size,1);
  const direct=vm.runInContext('refreshState()',refreshContext);
  assert.equal(timers.size,0,'轮询/主动刷新合并掉尚未触发的 SSE 刷新');
  assert.equal(stateRequests.length,3);
  stateS.cur='B';
  const switched=vm.runInContext('refreshState()',refreshContext);
  assert.equal(stateRequests.length,4);
  stateRequests[2].resolve({session:{status:'running',id:'A-old'}});await direct;
  assert.notEqual(stateS.state?.session.id,'A-old','旧会话在途结果不能覆盖新会话');
  stateRequests[3].resolve({session:{status:'running',id:'B'}});await switched;
  assert.equal(stateS.state.session.id,'B');
  stateS.cur='A';schedule();const staleTimer=[...timers.values()][0];
  stateS.cur='B';staleTimer.fn();await drain();
  assert.equal(stateRequests.length,4,'旧会话合并定时器不能触发新会话请求');
  stateDoc.visibilityState='hidden';schedule();
  assert.equal(stateRequests.length,4);
  console.log('polling: single scheduler, quiet fallback, visibility, session exit and in-flight dedup passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
