"use strict";
// 模拟 innerHTML 销毁子节点；真实布局和滚动范围另用浏览器验证。
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const esc=value=>String(value??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const flush=()=>new Promise(resolve=>setImmediate(resolve));
class Element{
  constructor(){this.dataset={};this.scrollTop=0;this.scrollLeft=0;this.children=new Map();this.pre=[];this.cards=[];this.buttons=[];this.proposals=[];this.classList={toggle(){}};}
  set innerHTML(html){
    this.html=html;this.children.clear();
    this.pre=[...html.matchAll(/<pre>/g)].map(()=>new Element());
    this.cards=[...html.matchAll(/<article class="exp-card" data-insight="([^"]*)">/g)].map(match=>{const card=new Element();card.dataset.insight=match[1];card.querySelector('details').open=false;return card;});
    this.buttons=[...html.matchAll(/data-background-select="([^"]*)"/g)].map(match=>{const button=new Element();button.dataset.backgroundSelect=match[1];return button;});
    this.proposals=[...html.matchAll(/<details class="resource-proposal" data-proposal="([^"]*)"([^>]*)>/g)].map(match=>{const box=new Element();box.dataset.proposal=match[1];box.open=match[2].includes('open');return box;});
  }
  get innerHTML(){return this.html||'';}
  get textContent(){return (this.html||'').replace(/<[^>]*>/g,'');}
  set textContent(value){this.innerHTML=String(value);}
  querySelector(selector){if(!this.children.has(selector))this.children.set(selector,new Element());return this.children.get(selector);}
  querySelectorAll(selector){
    if(selector==='pre')return this.pre;
    if(selector==='[data-insight]')return this.cards;
    if(selector==='[data-background-select]')return this.querySelector('[data-background-list]').buttons;
    if(selector==='[data-proposal]')return this.proposals;
    return [];
  }
  replaceChildren(){this.innerHTML='';}
}
const read=name=>fs.readFileSync(path.join(__dirname,'../src/spiritscout/web/static/js',name),'utf8');
async function background(){
  const root=new Element(),S={view:'session',tab:'background',cur:'one'},calls=[];
  let texts={a:'旧输出',b:'另一任务'},rows=[{job_id:'a',label:'任务 A',created_at:2,status:'running',archive_state:'complete',stdout_bytes:9,stderr_bytes:0},{job_id:'b',label:'任务 B',created_at:1,status:'completed',archive_state:'complete',stdout_bytes:12,stderr_bytes:0}];
  let outputReply=null;
  const context=vm.createContext(require('./helpers/i18n').context({S,esc,$:()=>root,URLSearchParams,FileCards:{sizeText:String},window:{getSelection:()=>null},setTimeout:()=>1,clearTimeout(){},
    api:async(url)=>{
      calls.push(url);const parts=url.split('/'),id=parts[5];
      if(url.includes('/output?'))return outputReply?outputReply(url):{text:url.includes('stream=stderr')?'':texts[id],start:0,end:rows.find(row=>row.job_id===id)?.stdout_bytes||0,total_bytes:rows.find(row=>row.job_id===id)?.stdout_bytes||0,has_earlier:false,match:null,archive_state:'complete'};
      if(url.includes('?offset='))return {items:rows.map(row=>({...row})),total:rows.length,counts:{running:1,completed:1},stored_bytes:1,limit_bytes:512};
      return {...rows.find(row=>row.job_id===id),command:'echo original'};
    }}));
  const api=vm.runInContext(read('background-tasks.js')+'\nBackgroundTasks',context);
  const click=attrs=>{const button={dataset:attrs,disabled:false,hasAttribute:name=>name==='data-background-refresh'&&attrs.refresh};return root.onclick({target:{closest:()=>button}});};
  api.open();await flush();await flush();
  const detail=root.querySelector('[data-background-detail]'),stdout=detail.querySelector('[data-stream="stdout"]'),first=stdout.querySelector('pre');
  first.scrollHeight=1000;first.clientHeight=200;first.scrollTop=180;first.scrollLeft=65;first.onscroll();
  await click({refresh:true});assert.equal(stdout.querySelector('pre'),first,'相同数据保留后台输出节点');
  rows[0].stdout_bytes=19;texts.a+='\n新增行';await click({refresh:true});
  assert.equal(first.textContent,'旧输出','查看历史时不覆盖输出或拉回底部');
  assert.equal(first.scrollTop,180);assert.equal(first.scrollLeft,65);
  await click({latest:'stdout'});assert.equal(first.textContent,'旧输出\n新增行');
  rows[0].stdout_bytes=30;texts.a+='\n继续输出';await click({refresh:true});
  assert.equal(stdout.querySelector('pre'),first,'实时输出也更新原节点，不破坏输入框或布局');
  assert.equal(first.textContent,texts.a);
  click({backgroundSelect:'b'});await flush();await flush();
  const second=detail.querySelector('[data-stream="stdout"]').querySelector('pre');assert.notEqual(second,first,'切换任务创建自己的输出窗口');
  assert.equal(second.scrollLeft,0,'滚动位置不跨任务继承');
  let resolve;outputReply=()=>new Promise(done=>{resolve=done;});const stale=click({latest:'stdout'});
  outputReply=null;click({backgroundSelect:'a'});await flush();await flush();
  resolve({text:'旧任务晚到输出',start:0,end:10,total_bytes:10,has_earlier:false});await stale;
  assert.equal(detail.querySelector('[data-stream="stdout"]').querySelector('pre').textContent,texts.a,'乱序输出不能覆盖新选中的任务');
  api.close();
}
async function experience(){
  const root=new Element(),S={view:'session',tab:'experience',cur:'one'};
  let data={state:{can_distill:true,reason:'可提炼'},items:[{insight_id:'stable',insight:'经验',sources:[{ref:'来源'}]}],history:[{result:'ok'}]};
  const context=vm.createContext(require('./helpers/i18n').context({S,esc,$:()=>root,modelDisplayName:String,api:async()=>data,toast(){}}));
  const api=vm.runInContext(read('session-experience.js')+'\nSessionExperience',context);
  await api.load();
  const history=root.querySelector('.exp-history');history.scrollTop=250;
  root.cards[0].querySelector('details').open=true;
  await api.load();assert.equal(root.querySelector('.exp-history'),history,'相同数据保留经验 DOM');
  const distill=root.querySelector('[data-distill]');await distill.onclick({currentTarget:distill});await flush();
  assert.equal(distill.disabled,false,'接口返回相同状态时恢复提炼按钮，避免 DOM 缓存留下禁用状态');
  data.state.reason='状态更新';
  data.items.unshift({insight_id:'new',insight:'新增经验',sources:[{ref:'新来源'}]});
  await api.load();
  assert.equal(root.querySelector('.exp-history').scrollTop,250);
  assert.equal(root.cards[0].querySelector('details').open,false,'新增条目使用默认状态');
  assert.equal(root.cards[1].querySelector('details').open,true,'条目重排后按 ID 保留展开状态');
  S.cur='two';await api.load();
  assert.equal(root.querySelector('.exp-history').scrollTop,0,'经验位置不跨会话继承');
  assert.equal(root.cards[1].querySelector('details').open,false);
  let resolve;context.api=()=>new Promise(done=>{resolve=done;});
  const old=api.load();S.cur='three';context.api=async()=>({...data,state:{reason:'新会话'}});
  await api.load();const html=root.innerHTML;resolve({state:{reason:'旧会话'},items:[],history:[]});await old;
  assert.equal(root.innerHTML,html,'旧会话响应不覆盖新会话');
}
function resources(){
  const root=new Element();let catalogs=0;
  const source=read('resources.js'),context=vm.createContext(require('./helpers/i18n').context({root:()=>root,esc,load:()=>{catalogs++;}}));
  vm.runInContext(source.slice(source.indexOf('  function renderChat'),source.indexOf('  async function loadChat')),context);
  const render=vm.runInContext('renderChat',context);
  const data={messages:[],proposals:[{id:'old',state:'completed',proposal:{text:'已应用'}},{id:'current',state:'pending',proposal:{text:'待确认'}}]};
  render('mcp',data);assert.equal(catalogs,1);
  const proposals=root.querySelector('.resource-proposals'),box=proposals.proposals[1];
  box.querySelector('pre').scrollTop=170;box.open=false;proposals.scrollTop=80;
  data.messages.push({role:'assistant',content:'新回复',created_at:1});render('mcp',data);
  assert.equal(proposals.proposals[1],box,'聊天更新不重建未变化的方案');
  assert.equal(catalogs,1,'已完成的旧方案不反复刷新目录、重置分页');
  data.proposals.reverse();data.proposals[0].proposal.text+=' 更新';render('mcp',data);
  assert.equal(proposals.proposals[0].open,false);
  assert.equal(proposals.proposals[0].querySelector('pre').scrollTop,170,'方案重排后按 ID 恢复预览位置');
  assert.equal(proposals.scrollTop,80);
  data.proposals[0].state='completed';render('mcp',data);
  assert.equal(catalogs,2,'新方案完成后仍刷新资源目录');
}
(async()=>{await background();await experience();resources();console.log('refresh scroll: unchanged DOM, output updates, keyed expansion, catalog pagination and session isolation passed');})().catch(error=>{console.error(error);process.exitCode=1;});
