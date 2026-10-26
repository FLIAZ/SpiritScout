/* 通过控制器验证旁路创建/续聊的 API 契约，以及导航后异步返回隔离。 */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
function element(){return {dataset:{},hidden:false,value:'',innerHTML:'',textContent:'',disabled:false,scrollTop:0,scrollHeight:0,clientHeight:0,classList:{toggle(){}},listeners:{},addEventListener(type,fn){this.listeners[type]=fn;},querySelector(s){return get(s);},querySelectorAll(){return [];},focus(){}};}
const elements=new Map();
function get(s){if(!elements.has(s))elements.set(s,element());return elements.get(s);}
const requests=[],S={cur:'parent',view:'session',tab:'chat',sessions:[]};
let nextResponse=null;
const context=vm.createContext({S,$:get,esc:String,mdRender:String,actorName:String,document:{activeElement:null},openSession(){throw Error('不应跳离主会话');},setTimeout(){return 1;},clearTimeout(){},api:async(url,options)=>{
 requests.push({url,body:options && JSON.parse(options.body)});
 if(nextResponse)return nextResponse;
 if(url.endsWith('/conversation'))return {session:{status:'completed',side_session:{task_id:'task'}},messages:[{role:'user',content:'第一问'},{role:'assistant',content:'第一答'}]};
 return {session_id:'child',side_session:{actor:'recon',parent_session_id:'parent'}};
}});
vm.runInContext(fs.readFileSync('src/spiritscout/web/static/js/agent-side-chat.js','utf8')+'\nglobalThis.chat=AgentSideChat;',context);
const agent={actor:'recon',taskId:'task',status:'执行中',tasks:new Map([['task',{id:'task'}]])};
const input=get('textarea'),form=get('form');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 context.chat.init();context.chat.sync([agent],{mode:'src'});context.chat.select('recon');
 input.value='第一问';input.listeners.input({target:input});await form.listeners.submit({preventDefault(){}});await tick();
 assert.deepEqual(requests.find(r=>r.url.endsWith('/agent-sessions')).body,{actor:'recon',task_id:'task',mission:'第一问'});
 assert.equal(S.cur,'parent');assert.match(get('.side-chat-messages').innerHTML,/第一答/);
 input.value='继续聊';input.listeners.input({target:input});await form.listeners.submit({preventDefault(){}});await tick();
 assert.deepEqual(requests.find(r=>r.url.endsWith('/messages')).body,{content:'继续聊'});
 assert.equal(requests.filter(r=>r.url.endsWith('/agent-sessions')).length,1);
 context.chat.select('');assert.equal(get('#session-observer').hidden,false);
 context.chat.sync([agent],{mode:'competition'});context.chat.select('recon');assert.equal(get('#agent-side-chat').hidden,true);
 context.chat.sync([agent],{mode:'ctf'});context.chat.select('recon');await tick();
 let release;nextResponse=new Promise(resolve=>{release=resolve;});
 input.value='旧会话问题';input.listeners.input({target:input});const sending=form.listeners.submit({preventDefault(){}});
 S.cur='another';context.chat.sync([agent],{mode:'src'});context.chat.select('recon');input.value='新会话草稿';input.listeners.input({target:input});
 release({});await sending;assert.equal(input.value,'新会话草稿');assert.equal(S.cur,'another');
 console.log('inline side chat: create, reply, resume payload, mode guard and navigation race passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
