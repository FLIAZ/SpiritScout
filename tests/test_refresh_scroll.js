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
  const root=new Element(),S={view:'session',tab:'background',cur:'one'};
  let data={items:[{job_id:'a',label:'任务 A',status:'running',returncode:null,stdout:'旧输出',stderr:'错误流'},{job_id:'b',label:'任务 B',status:'completed',returncode:0,stdout:'另一任务'}]};
  const context=vm.createContext(require('./helpers/i18n').context({S,esc,$:()=>root,api:async()=>data,setTimeout:()=>1,clearTimeout(){}}));
  const api=vm.runInContext(read('background-tasks.js')+'\nBackgroundTasks',context);
  api.open();await flush();
  const detail=root.querySelector('[data-background-detail]'),first=detail.pre[0];
  first.scrollTop=180;first.scrollLeft=65;detail.pre[1].scrollTop=90;
  await root.querySelector('[data-background-refresh]').onclick();
  assert.equal(detail.pre[0],first,'相同数据保留后台输出节点');
  data.items[0].stdout='旧输出\n新增行';
  await root.querySelector('[data-background-refresh]').onclick();
  assert.notEqual(detail.pre[0],first);
  assert.equal(detail.pre[0].scrollTop,180);assert.equal(detail.pre[0].scrollLeft,65);
  assert.equal(detail.pre[1].scrollTop,90,'stdout 与 stderr 分别保留位置');
  root.querySelectorAll('[data-background-select]')[1].onclick();await flush();
  assert.equal(detail.pre[0].scrollTop,0,'切换任务不复用另一条输出的位置');
  data={items:[]};await root.querySelector('[data-background-refresh]').onclick();
  assert.equal(detail.hidden,true);assert.equal(detail.pre.length,0);
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
