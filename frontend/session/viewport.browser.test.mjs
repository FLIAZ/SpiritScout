import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';
import ts from 'typescript';

const root = resolve(import.meta.dirname, '../..');
const staticRoot = resolve(root, 'src/spiritscout/web/static');
let server, browser, base;
const read = path => readFile(resolve(root, path), 'utf8');
const compile = source => ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText;
const environment = `
const $ = selector => document.querySelector(selector);
const I18n = {t:(source,...values)=>typeof source==='string'?source:source.reduce((text,part,i)=>text+part+(values[i]??''),''),html:(source,...values)=>typeof source==='string'?source:source.reduce((text,part,i)=>text+part+(values[i]??''),'')};
const S={cur:'one',view:'session',tab:'chat',think:new Map(),evSeen:0};
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const actorName=String,argBrief=(_name,args)=>JSON.stringify(args||{}),evElapsed=String;
const SessionObserver={push(){},reset(){},toolName:String,cleanText:String,resultSummary:detail=>String(detail.note||detail.result||'')};
const AgentWorkbench={push(){},reset(){}};
const ToolParameters={attach(){},reset(){},prune(){}};
let traceStick=true, rendered=0, requests=0, held=null, hold=false, fail=false;
function stopStreams(){} function startStreams(){}
const events=Array.from({length:2240},(_,i)=>({id:i+1,event:{type:i%2?'agent.reply':'user.message',actor:i%2?'master':'user',ts:String(i+1),detail:{text:'item '+(i+1)+' '+('阅读记录。'.repeat(i%11===0?240:16)),content:'item '+(i+1)+' '+('阅读记录。'.repeat(i%11===0?240:16))}}}));
async function api(url,{signal}={}) {
 requests++;
 const u=new URL(url,location.href),end=Number(u.searchParams.get('before')||events.length);
 const data=u.searchParams.has('types')?events.filter(x=>x.event.type==='user.message'):events;
 const selected=data.filter(x=>x.id<=end).slice(-Number(u.searchParams.get('limit')||160));
 const result={items:selected,cursor:end,older_cursor:(selected[0]?.id||1)-1,has_more:(selected[0]?.id||1)>1,activity_count:selected.length};
 if(hold&&u.searchParams.has('activity')&&u.searchParams.has('before'))return new Promise((resolve,reject)=>{
  held=()=>{held=null;hold=false;if(fail){fail=false;reject(new Error('fixture unavailable'));}else resolve(result);};
  signal?.addEventListener('abort',()=>{const error=new Error('aborted');error.name='AbortError';reject(error);},{once:true});
 });
 return result;
}
function renderAuditEvent(event,{observe=true,eventId}={}) {
 rendered++;
 if(TaskTrace.push(event,eventId))return;
 if(!['user.message','agent.reply'].includes(event.type))return;
 const text=event.detail.content||event.detail.text||event.detail.result||event.type;
 const extra=eventId%3===0?'<details><summary>展开明细</summary><pre>'+('执行结果\\n'.repeat(60))+'</pre></details>':'';
 const row=traceAppend(event,'<div class="bubble"><div class="btxt">'+esc(text)+'</div>'+extra+'</div>',event.type==='user.message'?'tl-user':'tl-conv');
 row.dataset.historyId=String(eventId);
}
`;

before(async () => {
  const views = await read('src/spiritscout/web/static/js/views.js');
  const renderer = views.slice(views.indexOf('let traceRenderTarget ='), views.indexOf('function traceTurn('));
  const viewport = compile(await read('frontend/session/viewport.ts')).replace("'@tanstack/virtual-core'", "'/virtual/index.js'");
  const bridge = compile(await read('frontend/session/bridge.ts')).replace("'./reader.ts'", "'/reader.js'");
  const css = (await Promise.all(['base.css','views.css','motion.css'].map(name=>readFile(resolve(staticRoot,'css',name),'utf8')))).join('\n') + await read('frontend/session/session.css');
  const routes = new Map([
    ['/environment.js', environment], ['/renderer.js',renderer], ['/viewport.js',viewport], ['/bridge.js',bridge],
    ['/reader.js',compile(await read('frontend/session/reader.ts'))],
    ['/markdown.js',await read('src/spiritscout/web/static/js/markdown.js')],
    ['/task-trace.js',await read('src/spiritscout/web/static/js/task-trace.js')],
    ['/stream-text.js',await read('src/spiritscout/web/static/js/stream-text.js')],
    ['/history.js',await read('src/spiritscout/web/static/js/session-history.js')],
    ['/style.css',css+'\nbody{padding:24px}#view-session{width:960px}.trace-wrap{height:600px!important}#trace-box{height:600px!important;flex:none!important}.trace-items>.tl{max-width:900px!important}.btxt{white-space:pre-wrap}\n'],
  ]);
  const html = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><link rel="stylesheet" href="/style.css">
   <div id="view-session" data-session-id="one"><div class="trace-wrap"><div class="trace" id="trace-box"></div><nav id="um-rail" hidden></nav><div id="um-preview" hidden></div></div></div>
   <div id="events"></div><button id="trace-jump"></button><div id="trace-history"><span data-history-status></span><button data-history="oldest"></button><button data-history="latest"></button><button data-history="retry"></button></div>
   <script src="/environment.js"></script><script src="/markdown.js"></script><script src="/stream-text.js"></script><script src="/renderer.js"></script><script src="/task-trace.js"></script><script src="/history.js"></script>
   <script type="module">import {createTraceViewport} from '/viewport.js';import {createReaderBridge} from '/bridge.js';window.TraceViewport=createTraceViewport(document.querySelector('#trace-box'));window.bridge=createReaderBridge();window.ready=true;</script>`;
  server=createServer(async(req,res)=>{
    try {
      const path=new URL(req.url,'http://localhost').pathname;
      if(path==='/'){res.setHeader('Content-Type','text/html');res.end(html);return;}
      if(/^\/virtual\/[\w-]+\.js$/.test(path)){
        res.setHeader('Content-Type','text/javascript');res.end((await read('node_modules/@tanstack/virtual-core/dist/esm/'+path.split('/').at(-1))).replaceAll('process.env.NODE_ENV','"production"'));return;
      }
      if(routes.has(path)){res.setHeader('Content-Type',path.endsWith('.css')?'text/css':'text/javascript');res.end(routes.get(path));return;}
      if(path==='/favicon.ico'){res.statusCode=204;res.end();return;}
      res.statusCode=404;res.end();
    }catch(error){res.statusCode=500;res.end(String(error));}
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));base='http://127.0.0.1:'+server.address().port;
  browser=await chromium.launch({headless:process.env.CI==='true'});
});
after(async()=>{await browser?.close();await new Promise(resolve=>server?.close(resolve));});

async function fixture(t, setup) {
  const page=await browser.newPage({viewport:{width:1100,height:800}}), errors=[];
  page.on('pageerror',e=>{errors.push(e.message);console.error('fixture page error:',e.message)});
  page.on('requestfailed',r=>console.error('fixture request failed:',r.url(),r.failure()?.errorText));
  page.on('console',m=>{if(m.type()==='error')console.error('fixture console:',m.text())});t.after(async()=>{await page.close();assert.deepEqual(errors,[]);});
  await page.goto(base);await page.waitForFunction(()=>window.ready,{},{timeout:5000});
  if(setup)await page.evaluate(setup);
  await page.evaluate(()=>SessionHistory.open());await stable(page);
  return page;
}
async function stable(page) {
  await page.evaluate(async()=>{for(let i=0;i<16;i++)await new Promise(requestAnimationFrame);});
}
async function expectStill(page, label) {
  const values=await page.evaluate(async()=>{
    const row=window.witness,b=$('#trace-box'),samples=[];
    for(let i=0;i<12;i++){await new Promise(requestAnimationFrame);if(!row?.isConnected)throw new Error('reading anchor unmounted');samples.push(row.getBoundingClientRect().top-b.getBoundingClientRect().top);}
    return {expected:window.witnessOffset,samples};
  });
  assert.ok(values.samples.every(value=>Math.abs(value-values.expected)<1.1),label+': '+JSON.stringify(values));
}
async function witness(page) {
  await stable(page);
  await page.evaluate(()=>{
    const b=$('#trace-box'),bounds=b.getBoundingClientRect();
    window.witness=TraceViewport.mounted().find(row=>{const r=row.getBoundingClientRect();return r.bottom>bounds.top+30&&r.top<bounds.bottom;});
    window.witnessOffset=window.witness.getBoundingClientRect().top-bounds.top;
  });
}

test('short conversations start at the top and do not jump when output arrives',async t=>{
 const page=await fixture(t,()=>{
  events.splice(0,events.length,
   {id:1,event:{type:'user.message',actor:'user',ts:'0',detail:{content:'根据已收集资产开始 SRC 测试'}}},
   {id:2,event:{type:'agent.reply',actor:'master',ts:'1',detail:{text:'已加载资产清单，正在准备测试'}}});
 });
 const top=()=>page.evaluate(()=>{
  const row=TraceViewport.mounted()[0], box=$('#trace-box');
  return row.getBoundingClientRect().top-box.getBoundingClientRect().top;
 });
 assert.ok(await top()<60,'first message is near the top, not pushed down by a viewport-sized spacer');
 await page.evaluate(()=>SessionHistory.enqueue({type:'agent.reply',actor:'master',ts:'2',detail:{text:'优先核对预发入口'}},3,'one'));
 await page.waitForFunction(()=>S.evSeen===3);await stable(page);
 assert.ok(await top()<60,'a short appended reply keeps the conversation at the top');
 assert.equal(await page.evaluate(()=>TraceViewport.rows().length),3);
});

test('continuous history keeps measured anchors and bounds mounted DOM across old raw-cache eviction',async t=>{
 const page=await fixture(t);
 for(let i=0;i<13;i++){
  await page.evaluate(()=>{hold=true;traceStopFollowing();$('#trace-box').scrollTop=0;});
  await stable(page);
  if(!await page.evaluate(()=>!!held))await page.locator('#trace-box').dispatchEvent('wheel',{deltaY:-80,bubbles:true});
  await page.waitForFunction(()=>!!held);await witness(page);
  await page.evaluate(()=>{window.previousRows=TraceViewport.rows().slice();held();});
  await page.waitForFunction(()=>!SessionHistory.isRendering());
  await expectStill(page,'prepend '+i);
  assert.ok(await page.evaluate(()=>window.previousRows.every(row=>TraceViewport.has(row))));
 }
 const before=await page.evaluate(()=>({rendered,requests,count:TraceViewport.rows().length}));
 assert.equal(before.count,2240);assert.equal(before.rendered,2240,'each audit event is projected once');
 for(const fraction of [1,.7,.3,.85,.1,1,.4,0,1]){
  await page.evaluate(f=>{traceStopFollowing();const b=$('#trace-box');b.scrollTop=(b.scrollHeight-b.clientHeight)*f;},fraction);
  await witness(page);await expectStill(page,'scroll settles '+fraction);
  assert.ok(await page.evaluate(()=>TraceViewport.mounted().length<45),'only viewport and overscan are mounted');
 }
 assert.deepEqual(await page.evaluate(()=>({rendered,requests,count:TraceViewport.rows().length})),before,'scrolling never replays or rereads loaded history');
 await page.evaluate(()=>bridge.filter('no-such-message'));await stable(page);
 assert.equal(await page.evaluate(()=>TraceViewport.mounted().length),0);
 await page.evaluate(()=>bridge.filter(''));await stable(page);
 assert.equal(await page.evaluate(()=>TraceViewport.rows().length),2240);
});

test('streaming, expansion and layout resize preserve history readers; end-pinned readers follow output',async t=>{
 const page=await fixture(t);
 await page.evaluate(()=>{traceStopFollowing();$('#trace-box').scrollTop=2500;});await witness(page);
 await page.evaluate(()=>SessionHistory.enqueue({type:'agent.reply',actor:'master',ts:'new',detail:{text:'new response'}},3000,'one'));
 await page.waitForFunction(()=>S.evSeen===3000);await expectStill(page,'incoming response');
 await page.evaluate(()=>{
   const above=TraceViewport.mounted().filter(row=>row.getBoundingClientRect().bottom<=window.witness.getBoundingClientRect().top).at(-1);
   const body=above.querySelector('.btxt');
   StreamText.plain(body,body.textContent+'\n'+('streamed output\n'.repeat(50)),{animate:true});
   if(!body.querySelector('.stream-text-chunk'))throw new Error('missing stream animation');
 });
 await expectStill(page,'above-viewport growth');
 await page.evaluate(()=>{
   const above=TraceViewport.mounted().filter(row=>row.getBoundingClientRect().bottom<=window.witness.getBoundingClientRect().top&&row.querySelector('details')).at(-1);
   if(!above)throw new Error('missing expansion fixture');
   above.querySelector('details').open=true;
 });
 await expectStill(page,'expanded details above viewport');
 await page.setViewportSize({width:1200,height:850});await expectStill(page,'resize');
 await page.evaluate(()=>SessionHistory.latest());await stable(page);
 assert.ok(await page.evaluate(()=>TraceViewport.isAtEnd()));
 await page.evaluate(()=>{const body=TraceViewport.rows().at(-1).querySelector('.btxt');StreamText.plain(body,body.textContent+'\n'+('streamed output\n'.repeat(80)),{animate:true});});
 await stable(page);assert.ok(await page.evaluate(()=>TraceViewport.isAtEnd()),'streamed growth follows only at the end');
});

test('tool-heavy short batches prepend without collapsing the reading viewport',async t=>{
 const page=await fixture(t,()=>{
   events.splice(0,events.length,...Array.from({length:480},(_,i)=>({id:i+1,event:{type:'model.attempt_started',actor:'worker',seq:i+1,ts:String(i+1),detail:{}}})));
   events[0].event={type:'user.message',actor:'user',ts:'1',detail:{content:'old instructions\n'.repeat(150)}};
   events[198].event={type:'agent.turn',actor:'worker',seq:199,ts:'199',detail:{task_id:'shared',route_id:'route',turn:1,angle:'核对返回证据'}};
   events[199].event={type:'agent.tool_call',actor:'worker',seq:200,ts:'200',detail:{task_id:'shared',route_id:'route',calls:[{id:'call',name:'shell',preview:'complete arguments',arguments:{command:'fixture'}}]}};
   events[479].event={type:'agent.tool_result',actor:'worker',seq:480,ts:'480',detail:{task_id:'shared',route_id:'route',id:'call',name:'shell',ok:true,result:'completed response'}};
 });
 for(let i=0;i<2;i++){
   await page.evaluate(()=>{hold=true;traceStopFollowing();$('#trace-box').scrollTop=0;});await stable(page);
   if(!await page.evaluate(()=>!!held))await page.locator('#trace-box').dispatchEvent('wheel',{deltaY:-80,bubbles:true});
   await page.waitForFunction(()=>!!held);await witness(page);
   await page.evaluate(()=>{window.previousRows=TraceViewport.rows().slice();held();});
   await page.waitForFunction(()=>!SessionHistory.isRendering());await expectStill(page,'short batch '+i);
   assert.ok(await page.evaluate(()=>window.previousRows.every(row=>TraceViewport.has(row))));
 }
 const tool=await page.evaluate(()=>TraceViewport.rows().flatMap(row=>[...row.querySelectorAll('.tl-act')]).map(el=>({arguments:el.querySelector('.ar').textContent,result:el.querySelector('.tl-output').textContent})));
 assert.ok(tool.length>0);assert.ok(tool.every(item=>item.arguments==='complete arguments'&&item.result==='completed response'));
});

test('bookmark selection follows the requested message before and after older pages load',async t=>{
 const page=await fixture(t);
 for(const id of [2101,2181,1981,2101]){
   await page.locator(`#um-rail .um-bar[data-history-id="${id}"]`).click();
   await page.waitForFunction(id=>{
     if(SessionHistory.isRendering())return false;
     const active=document.querySelector('#um-rail .um-bar.active');
     const row=document.querySelector(`#trace-box .tl-user[data-history-id="${id}"]`);
     const rect=row?.getBoundingClientRect(),box=$('#trace-box').getBoundingClientRect();
     return active?.dataset.historyId===String(id)&&rect&&rect.bottom>box.top&&rect.top<box.bottom;
   },id,{timeout:4000});
 }
});

test('failed and stale history requests preserve the current conversation',async t=>{
 const page=await fixture(t);
 await page.evaluate(()=>{hold=true;fail=true;traceStopFollowing();$('#trace-box').scrollTop=0;});await stable(page);
 if(!await page.evaluate(()=>!!held))await page.locator('#trace-box').dispatchEvent('wheel',{deltaY:-80,bubbles:true});
 await page.waitForFunction(()=>!!held);await witness(page);
 const old=await page.evaluate(()=>TraceViewport.rows().length);
 await page.evaluate(()=>held());await page.waitForFunction(()=>!SessionHistory.isRendering());
 await expectStill(page,'load failure');assert.equal(await page.evaluate(()=>TraceViewport.rows().length),old);
 assert.match(await page.locator('[data-history-status]').innerText(),/读取失败/);
 await page.evaluate(()=>{hold=true;});await page.locator('#trace-box').dispatchEvent('wheel',{deltaY:-80,bubbles:true});
 await page.waitForFunction(()=>!!held);
 await page.evaluate(async()=>{SessionHistory.cancel();S.cur='two';$('#view-session').dataset.sessionId='two';await SessionHistory.open();held?.();});
 await stable(page);assert.equal(await page.evaluate(()=>TraceViewport.rows().length),160);
 assert.equal(await page.evaluate(()=>S.cur),'two');
});

test('统一动效：折叠、弹层和实况栏保留状态，关闭与系统减弱立即生效', async () => {
 const page = await browser.newPage({viewport:{width:1440,height:900},reducedMotion:'no-preference'});
 try {
  const styles = (await Promise.all(['base.css','views.css','motion.css'].map(name=>readFile(resolve(staticRoot,'css',name),'utf8')))).join('\n');
  await page.setContent(`<!doctype html><html data-motion="off"><style>${styles}</style>
   <details id="fold"><summary>详情</summary><div style="height:160px;position:relative"><button id="fold-child">内容</button><button id="floating-option" style="position:absolute;top:170px;left:0;z-index:20">下拉选项</button></div></details>
   <div class="tl-act" id="tool"><button class="hd">工具输出</button><pre>执行结果\n执行结果\n执行结果\n执行结果</pre></div>
   <section class="sess-folder folded"><button>项目</button><div class="sess-folder-body" inert><button style="height:120px">会话</button></div></section>
   <button id="trigger">打开设置</button><dialog id="modal"><button id="modal-child">弹层</button></dialog><div id="popover" popover><button>设置</button></div>
   <div id="view-session"><div id="tab-chat" class="rail-collapsed" style="height:180px;width:800px">
    <div class="trace-wrap"></div><aside class="session-rail" inert><textarea>未发送草稿</textarea></aside></div></div>`);
  let requests=0;page.on('request',()=>requests++);
  const frames = () => page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  const finish = async () => {await frames();await page.evaluate(()=>{for(const animation of document.getAnimations())if(animation.effect.getTiming().iterations!==Infinity)animation.finish();});await frames();};
  const middle = async selector => {
   await frames();const durations=await page.evaluate(selector=>{
    const target=document.querySelector(selector),animations=document.getAnimations().filter(a=>a.effect.target===target);
    for(const animation of animations){animation.pause();animation.currentTime=animation.effect.getTiming().duration/2;}
    return animations.map(a=>a.effect.getTiming().duration);
   },selector);await frames();return durations;
  };
  for(const [mode,duration] of [['auto',200],['soft',280],['quick',70]]) {
   await page.evaluate(mode=>document.documentElement.dataset.motion=mode,mode);await finish();
   for(const open of [true,false]) {
    const result=await page.$eval('#fold',async(el,{open,duration})=>{
     const start=el.getBoundingClientRect().height;el.open=open;const heights=[];let begin;
     await new Promise(resolve=>requestAnimationFrame(function sample(time){begin??=time;heights.push(el.getBoundingClientRect().height);
      if(time-begin>duration+40)resolve();else requestAnimationFrame(sample);
     }));
     return {start,heights,duration:getComputedStyle(el,'::details-content').transitionDuration};
    },{open,duration});
    assert.equal(parseFloat(result.duration)*1000,duration);
    const end=result.heights.at(-1);
    assert(result.heights.some(value=>value>Math.min(result.start,end)+.1&&value<Math.max(result.start,end)-.1),'详情展开和收起均有中间帧');
    if(open){
     assert.equal(await page.$eval('#floating-option',el=>{const box=el.getBoundingClientRect();return document.elementFromPoint(box.x+box.width/2,box.y+box.height/2)===el;}),true,'展开结束后内部下拉选项不能被裁剪');
     await page.$eval('#fold > div',el=>el.append('新输出'));assert.equal(await page.$eval('#fold',el=>getComputedStyle(el,'::details-content').opacity),'1','刷新正文不回放');
    }
   }
   await page.$eval('#tool',el=>el.classList.add('open'));
   assert((await middle('#tool > pre')).includes(duration));await finish();
   await page.$eval('#tool > pre',el=>el.append('新输出'));
   assert.equal(await page.$eval('#tool > pre',el=>el.getAnimations().length),0,'工具流式追加不回放');
   await page.$eval('#tool',el=>el.classList.remove('open'));await finish();
   assert.equal(await page.$eval('#tool > pre',el=>el.getBoundingClientRect().height),0);
   await page.$eval('.sess-folder',el=>{el.classList.remove('folded');el.lastElementChild.inert=false;});
   assert((await middle('.sess-folder-body')).includes(duration));
   const height=await page.$eval('.sess-folder-body',el=>el.getBoundingClientRect().height);assert(height>0&&height<120);await finish();
   await page.$eval('.sess-folder',el=>{el.classList.add('folded');el.lastElementChild.inert=true;});await finish();
   assert.equal(await page.$eval('.sess-folder-body',el=>el.getBoundingClientRect().height),0);
   for(const [selector,open,close] of [['#modal','showModal','close'],['#popover','showPopover','hidePopover']]) {
    await page.locator('#trigger').focus();
    await page.$eval(selector,(el,method)=>el[method](),open);assert((await middle(selector)).includes(duration));await finish();
    if(selector==='#modal')assert.equal(await page.$eval(selector,el=>getComputedStyle(el,'::backdrop').transitionDuration.split(',')[0]),`${duration/1000}s`);
    await page.$eval(selector,(el,method)=>el[method](),close);await page.$eval(`${selector} button`,el=>el.focus());
    assert.equal(await page.evaluate(selector=>document.querySelector(selector).contains(document.activeElement),selector),false,`${selector} 关闭不延迟交互回收`);
    assert((await middle(selector)).includes(duration));await finish();assert.equal(await page.$eval(selector,el=>getComputedStyle(el).display),'none');
   }
   await page.$eval('#tab-chat',el=>{el.classList.remove('rail-collapsed');el.lastElementChild.inert=false;});
   assert((await middle('#tab-chat')).includes(duration));await finish();
   await page.$eval('#tab-chat',el=>{el.classList.add('rail-collapsed');el.lastElementChild.inert=true;});await finish();
   assert.equal(await page.inputValue('.session-rail textarea'),'未发送草稿');
  }
  for(const reduced of [false,true]) {
   await page.evaluate(reduced=>document.documentElement.dataset.motion=reduced?'soft':'off',reduced);
   await page.emulateMedia({reducedMotion:reduced?'reduce':'no-preference'});
   await page.$eval('#fold',el=>el.open=true);await page.$eval('#modal',el=>el.showModal());await frames();
   assert.equal(await page.evaluate(()=>document.getAnimations().length),0);
   assert.equal(await page.$eval('#fold',el=>getComputedStyle(el,'::details-content').transitionProperty),'none');
   await page.$eval('#modal',el=>el.close());await page.$eval('#fold',el=>el.open=false);
  }
  assert.equal(requests,0,'动效不新增数据或模型请求');
 } finally {await page.close();}
});

test('流式正文只渐显新增文本，保留旧节点、选区、Markdown 与阅读位置', async () => {
 const page=await browser.newPage({viewport:{width:1440,height:900},reducedMotion:'no-preference'});
 try {
  const styles=(await Promise.all(['base.css','views.css','motion.css'].map(name=>readFile(resolve(staticRoot,'css',name),'utf8')))).join('\n');
  await page.setContent(`<!doctype html><html data-motion="off"><style>${styles}</style><div id="trace-box" style="height:500px;overflow:auto"></div><button id="trace-jump"></button><div id="events"></div><section id="view-session"><div id="plain" class="tl-say"></div><div id="markdown" class="md"></div></section>`);
  await page.addScriptTag({content: await read('src/spiritscout/web/static/js/stream-text.js')});
  await page.addScriptTag({content: `const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));`});
  await page.addScriptTag({content: await read('src/spiritscout/web/static/js/markdown.js')});
  let requests=0;page.on('request',()=>requests++);
  const flush=()=>page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  const finish=async()=>{await page.evaluate(async()=>{for(const animation of document.getAnimations())if(animation.effect.getTiming().iterations!==Infinity)animation.finish();await Promise.resolve();});await flush();};
  for(const [mode,duration] of [['auto',140],['soft',180],['quick',70]]) {
   await page.evaluate(mode=>{document.documentElement.dataset.motion=mode;document.getElementById('plain').replaceChildren();},mode);await flush();
   await page.evaluate(()=>StreamText.plain(document.getElementById('plain'),'已经展示的正文。'));await flush();
   await page.evaluate(()=>{
    const box=document.getElementById('plain');window.originalText=box.firstChild;
    const range=document.createRange();range.setStart(box.firstChild,0);range.setEnd(box.firstChild,4);const selection=document.getSelection();selection.removeAllRanges();selection.addRange(range);
    StreamText.plain(box,'已经展示的正文。新收到的内容。',{animate:true});
   });
   assert.equal(await page.textContent('#plain'),'已经展示的正文。新收到的内容。','收到的文本无需等待动画便完整可读');
   assert.equal(await page.evaluate(()=>document.getSelection().toString()),'已经展示');
   assert.equal(await page.evaluate(()=>document.getElementById('plain').firstChild===originalText),true);
   assert.equal(await page.textContent('.stream-text-chunk'),'新收到的内容。');
   const timing=await page.evaluate(()=>{const animation=document.querySelector('.stream-text-chunk').getAnimations()[0];animation.pause();animation.currentTime=animation.effect.getTiming().duration/2;return animation.effect.getTiming().duration;});
   assert.equal(timing,duration);const opacity=Number(await page.$eval('.stream-text-chunk',el=>getComputedStyle(el).opacity));assert(opacity>.35&&opacity<1);
   await finish();assert.equal(await page.locator('.stream-text-chunk').count(),0);assert.equal(await page.evaluate(()=>document.getSelection().toString()),'已经展示');
   await page.evaluate(()=>{
    const box=document.getElementById('markdown');box.replaceChildren();
    StreamText.markdown(box,mdRender('## 标题\n\n旧的 **粗体** 和正文。'));window.oldParagraph=box.querySelector('p');window.oldBold=box.querySelector('strong,b');
    StreamText.markdown(box,mdRender('## 标题\n\n旧的 **粗体** 和正文。继续输出。'),{animate:true});
   });
   assert.equal(await page.evaluate(()=>document.getElementById('markdown').querySelector('p')===oldParagraph),true);
   assert.equal(await page.evaluate(()=>document.getElementById('markdown').querySelector('strong,b')===oldBold),true);
   assert.equal(await page.textContent('.stream-text-chunk'),'继续输出。');await finish();
   await page.evaluate(()=>{
    const box=document.getElementById('markdown');
    StreamText.markdown(box,mdRender('## 标题\n\n旧的 **粗体** 和正文。继续输出。\n\n```js\nconst a = 1;\n```\n\n| 列 | 值 |\n|---|---|\n| 1 | 2 |'),{animate:true});
   });
   assert.equal(await page.locator('pre .stream-text-chunk,table .stream-text-chunk').count(),0,'代码与表格不拆字动画');await finish();
   assert.equal(await page.$eval('#markdown',el=>el.innerHTML),await page.evaluate(()=>{const expected=document.createElement('div');expected.innerHTML=mdRender('## 标题\n\n旧的 **粗体** 和正文。继续输出。\n\n```js\nconst a = 1;\n```\n\n| 列 | 值 |\n|---|---|\n| 1 | 2 |');return expected.innerHTML;}),'收尾 DOM 与原 Markdown 渲染等价');
   await page.evaluate(()=>StreamText.plain(document.getElementById('plain'),'正文被修正',{animate:true}));assert.equal(await page.locator('.stream-text-chunk').count(),0,'修正不重播旧文字');
  }
  // 密集分片只有有限的正在渐显尾部，收尾后恢复单一文本节点。
  const dense=await page.evaluate(async()=>{
   const box=document.getElementById('plain');box.replaceChildren();document.getSelection().removeAllRanges();let text='';let high=0;
   const start=performance.now();
   for(let batch=0;batch<20;batch++){
    for(let i=0;i<20;i++){text+='分片。';StreamText.plain(box,text,{animate:true});high=Math.max(high,box.childNodes.length);}
    for(const a of box.getAnimations({subtree:true}))a.finish();await Promise.resolve();
   }
   return {text:box.textContent,children:box.childNodes.length,high,elapsed:performance.now()-start};
  });
  assert.equal(dense.text,'分片。'.repeat(400));assert.equal(dense.children,1);assert(dense.high<=21);console.log('stream rendering measurement:',{children:dense.children,high:dense.high,elapsed:dense.elapsed});
  await page.evaluate(()=>{
   const box=document.getElementById('markdown');
   StreamText.markdown(box,mdRender('旧的 粗体'),{animate:false});
   StreamText.markdown(box,mdRender('旧的 **粗体** 新文字'),{animate:true});
  });
  assert.equal(await page.textContent('.stream-text-chunk'),' 新文字','Markdown 边界变化不重播旧正文');await finish();
  await page.evaluate(()=>{
   const detached=document.createElement('div');StreamText.plain(detached,'离屏正文',{animate:true});
   document.getElementById('plain').replaceChildren(detached);
  });
  assert.equal(await page.locator('.stream-text-chunk').count(),0,'离屏渲染和重新挂载不重播');
  await page.evaluate(()=>{
   const box=document.getElementById('plain');box.replaceChildren();StreamText.plain(box,'可选择的新内容',{animate:true});
   const range=document.createRange();range.selectNodeContents(box.querySelector('.stream-text-chunk'));
   document.getSelection().removeAllRanges();document.getSelection().addRange(range);
  });
  await finish();assert.equal(await page.evaluate(()=>document.getSelection().toString()),'可选择的新内容','动画收尾不破坏新文字选区');
  await page.evaluate(()=>document.getSelection().removeAllRanges());
  // 动效关闭、系统减弱或切换为关闭时，立即收敛到原文。
  for(const reduced of [false,true]){
   await page.evaluate(reduced=>document.documentElement.dataset.motion=reduced?'soft':'off',reduced);await page.emulateMedia({reducedMotion:reduced?'reduce':'no-preference'});
   await page.evaluate(()=>StreamText.plain(document.getElementById('plain'),'即时内容',{animate:true}));assert.equal(await page.locator('.stream-text-chunk').count(),0);
  }
  await page.emulateMedia({reducedMotion:'no-preference'});await page.evaluate(()=>document.documentElement.dataset.motion='soft');await flush();
  await page.evaluate(()=>{StreamText.plain(document.getElementById('plain'),'即时内容渐显中',{animate:true});document.documentElement.dataset.motion='off';});await flush();
  assert.equal(await page.locator('.stream-text-chunk').count(),0);assert.equal(await page.textContent('#plain'),'即时内容渐显中');
  // 实际主会话与任务 Markdown 渲染器，历史回放不播放动画。
  const views=await read('src/spiritscout/web/static/js/views.js');
  const section=(start,end)=>views.slice(views.indexOf(start),views.indexOf(end));
  await page.addScriptTag({content:`const $=selector=>document.querySelector(selector);const S={cur:'one',view:'session',tab:'chat',state:null,sessions:[],think:new Map()};const I18n={t:(source,...values)=>typeof source==='string'?source:source.reduce((text,part,i)=>text+part+(values[i]??''),''),html:(source,...values)=>typeof source==='string'?source:source.reduce((text,part,i)=>text+part+(values[i]??''),'')};const actorName=String,evElapsed=String,fmtK=String,TERMINAL=new Set();const SessionObserver={push(){},reset(){},toolName:String,cleanText:String,resultSummary:()=>''};const AgentWorkbench={push(){},reset(){}};const ToolParameters={attach(){}};function argBrief(){return ''}`});
  await page.addScriptTag({content:await read('src/spiritscout/web/static/js/task-trace.js')});
  await page.addScriptTag({content:section('let traceStick = true;','function argBrief(')+section('function traceRetryStatus(','const traceBox = $("#trace-box");')});
  await page.evaluate(()=>document.documentElement.dataset.motion='auto');await flush();
  await page.evaluate(()=>{tracePush({type:'agent.delta',actor:'master',detail:{kind:'text',text:'主会话正文'}});tracePush({type:'agent.delta',actor:'recon',detail:{kind:'text',text:'任务说明。第一段'},ts:'1'});TaskTrace.flush();});
  assert.equal(await page.locator('#trace-box .stream-text-chunk').count(),2,'两条实际流式路径都渐显');await finish();
  await page.evaluate(()=>{tracePush({type:'agent.delta',actor:'recon',detail:{kind:'text',text:'继续'},ts:'2'});TaskTrace.flush();});assert.equal(await page.textContent('#trace-box .stream-text-chunk'),'继续');await finish();
  await page.evaluate(()=>{tracePush({type:'agent.delta',actor:'master',detail:{kind:'text',text:'历史'},ts:'3'},{observe:false,animate:false});tracePush({type:'agent.delta',actor:'recon',detail:{kind:'text',text:'历史'},ts:'4'},{observe:false,animate:false});TaskTrace.flush();});assert.equal(await page.locator('.stream-text-chunk').count(),0);
  await page.evaluate(()=>{
   traceReset();document.getElementById('trace-box').replaceChildren();
   tracePush({type:'agent.delta',actor:'recon',detail:{kind:'text',text:'核对 `profile` 的 **显示格式**'},ts:'5'});TaskTrace.flush();
  });
  const heading=page.locator('.task-stage .task-heading > strong');
  assert.equal(await heading.textContent(),'核对 profile 的 显示格式');
  assert.equal(await heading.locator('code').textContent(),'profile');
  assert.equal(await heading.locator('b').textContent(),'显示格式');
  assert.notEqual(await heading.locator('code').evaluate(el=>getComputedStyle(el).backgroundColor),'rgba(0, 0, 0, 0)');
  assert.equal(await page.locator('.task-stage .task-step').evaluate(el=>el.open),false);
  assert.equal(await page.locator('.task-stage-summary[hidden]').count(),1);
  assert.equal(await page.locator('.stream-text-chunk').count(),0);
  await page.evaluate(()=>{tracePush({type:'agent.delta',actor:'recon',detail:{kind:'text',text:'继续'},ts:'6'});TaskTrace.flush();});
  assert.equal(await page.textContent('.stream-text-chunk'),'继续','原先隐藏的摘要显示时只渐显新增正文');await finish();
  await page.evaluate(()=>{tracePush({type:'agent.delta',actor:'master',detail:{kind:'text',text:'将被清理'},ts:'7'});tracePush({type:'agent.delta',actor:'master',detail:{kind:'reset'},ts:'8'});});
  await finish();assert.equal(await page.locator('#trace-box .tl-say').count(),0,'流重置不残留动画正文');
  assert.equal(requests,0,'正文动效不新增数据或模型请求');
 }finally{await page.close();}
});
