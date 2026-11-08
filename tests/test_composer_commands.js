const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor() { this.value='';this.attrs={};this.style={};this.listeners={};this.children=[];this.hidden=false;this.dataset={}; }
  setAttribute(k,v) { this.attrs[k]=v; }
  removeAttribute(k) { delete this.attrs[k]; }
  addEventListener(k,f) { (this.listeners[k] ||= []).push(f); }
  emit(k,event={}) { for(const f of this.listeners[k] || []) f(event); }
  before(...nodes) { this.siblings=nodes; }
  set innerHTML(value) {
    this.html=value;
    this.children=[...value.matchAll(/<button[^>]*>/g)].map(([tag])=>{
      const button=new Element();button.id=/id="([^"]+)"/.exec(tag)?.[1];
      button.dataset.command=/data-command="([^"]+)"/.exec(tag)?.[1];return button;
    });
  }
  get innerHTML() { return this.html || ''; }
  querySelectorAll() { return this.children; }
  querySelector() { return this.children[0]; }
  getBoundingClientRect() { return {top:500,bottom:540,left:20,width:640}; }
  get offsetHeight() { return 320; }
  scrollIntoView() {}
  focus() {}
  setSelectionRange() {}
  click() { this.clicked=true; }
}
const nodes=new Map(), $=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
const s={session_id:'a',status:'running',mode:'ctf',paused:false,title:'fixture'};
const S={cur:'a',view:'session',state:{session:s},sessions:[s],compFiles:[],homeFiles:[]};
let requests=[],fail=false,resolve,delay=false,uploads=0,toasts=[];
const ctx=vm.createContext(require('./helpers/i18n').context({S,$,console,
  document:{createElement:()=>new Element(),addEventListener(){}},
  window:{innerWidth:1000,innerHeight:800,addEventListener(){}},
  esc:value=>String(value).replaceAll('<','&lt;').replaceAll('>','&gt;'),
  toast:(...args)=>toasts.push(args),compGrow(){},homeSync(){},syncComposerControl(){},
  sessionMessagePending:new Set(),sessionControlPending:new Set(),TERMINAL:new Set(['completed']),
  STATUS_CN:{running:'运行中'},MODE_CN:{ctf:'CTF'},refreshState(){},refreshSessions(){},
  startStreams(){},renderFileChips(){},compInput:$('#comp-input'),
  stageFiles:async()=>{uploads++;return [];},
  api:async(url,opts)=>{
    requests.push({url,body:opts?.body && JSON.parse(opts.body)});
    if(fail){fail=false;throw new Error('fixture failure');}
    if(delay){delay=false;await new Promise(r=>{resolve=r;});}
    if(url.endsWith('/context'))return {session:s,request:null,results:[]};
    if(url.endsWith('/pause'))return {...s,paused:true};
    if(url.endsWith('/resume'))return {...s,paused:false};
    return {};
  }
}));
vm.runInContext(fs.readFileSync('src/spiritscout/web/static/js/composer-commands.js','utf8'),ctx);
const commands=vm.runInContext('ComposerCommands',ctx);
const source=fs.readFileSync('src/spiritscout/web/static/js/console.js','utf8');
vm.runInContext(source.slice(source.indexOf('const composerDeliveries'),source.indexOf('/* ================= 发起会话')),ctx);
const input=$('#comp-input'), panel=input.siblings[1], menu=input.siblings[0];
const key=(key,extra={})=>({key,preventDefault(){this.prevented=true;},...extra});
(async()=>{
  for(const text of ['https://target/path','/tmp/file.txt','/api/health details','inspect /compact','/compact\nnotes','//compact'])assert.equal(commands.parse(text),null,text);
  assert.equal(commands.parse(' /COMPACT ').name,'compact');
  assert.equal(commands.parse('/compact extra').args,'extra');
  input.value='/';input.emit('input');assert.equal(menu.hidden,false);
  assert.equal(input.attrs['aria-expanded'],'true');
  commands.keydown(key('ArrowDown'),'session');
  commands.keydown(key('Tab'),'session');assert.equal(input.value,'/status');assert.equal(menu.hidden,true);
  input.value='/co';input.emit('input');commands.keydown(key('Enter'),'session');
  assert.equal(input.value,'/compact');assert.equal(requests.length,0,'补全不执行');
  input.value='/';input.emit('input');
  assert.equal(commands.keydown(key('Enter',{isComposing:true}),'session'),false);
  assert.equal(commands.keydown(key('Enter',{shiftKey:true}),'session'),false);
  commands.keydown(key('Escape'),'session');assert.equal(menu.hidden,true);
  input.value='/compact';S.compFiles=[{name:'fixture.txt'}];await ctx.compSend();
  assert.equal(requests.length,0);assert.equal(uploads,0);assert.equal(S.compFiles.length,1);
  S.compFiles=[];input.value='/compact extra';await ctx.compSend();
  input.value='/unknown';await ctx.compSend();assert.equal(requests.length,0);
  input.value='/compact';fail=true;await ctx.compSend();assert.equal(input.value,'/compact');
  const requestId=requests.at(-1).body.request_id;
  await ctx.compSend();assert.equal(requests.at(-1).body.request_id,requestId);assert.equal(input.value,'');
  assert.equal(uploads,0,'命令不走附件或普通消息入口');
  assert.ok(requests.every(r=>r.url.endsWith('/compact')));
  assert.ok(panel.innerHTML.includes('下一次模型调用'));
  input.value='/status';await ctx.compSend();assert.ok(panel.innerHTML.includes('运行中'));
  input.value='/pause';await ctx.compSend();assert.equal(s.paused,true);
  input.value='/resume';await ctx.compSend();assert.equal(s.paused,false);
  input.value='/model';await ctx.compSend();assert.equal($('#comp-model').clicked,true);
  input.value='/files';await ctx.compSend();assert.equal($('#s-tabs [data-tab="deliverables"]').clicked,true);
  input.value='/report';await ctx.compSend();assert.equal($('#s-tabs [data-tab="report"]').clicked,true);
  input.value='//compact';await ctx.compSend();assert.equal(requests.at(-1).body.content,'/compact');
  input.value='/tmp/file.txt';await ctx.compSend();assert.equal(requests.at(-1).body.content,'/tmp/file.txt');
  input.value='/compact';delay=true;const pending=ctx.compSend();
  await ctx.compSend();assert.equal(requests.at(-1).url,'/api/sessions/a/compact');
  S.cur='b';commands.reset();input.value='新会话草稿';resolve();await pending;
  assert.equal(input.value,'新会话草稿');assert.equal(panel.hidden,true);
  const count=requests.length;S.view='home';$('#c-mission').value='/compact';
  assert.equal(await commands.execute('/compact',[],'home'),true);assert.equal(requests.length,count);
  $('#c-mission').value='/help';await commands.execute('/help',[],'home');assert.equal($('#c-mission').value,'');
  console.log('composer commands: keyboard, IME, routing, attachments, retry identity, session isolation and home passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
