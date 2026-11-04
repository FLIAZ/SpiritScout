"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('src/spiritscout/web/static/js/console.js','utf8');
const hints=source.slice(source.indexOf('function ctfTypeHintPayload'),source.indexOf('$("#c-mode").onchange'));
const launch=source.slice(source.indexOf('async function launchMission'),source.indexOf('/* 规则冲突即时确认'));
const nodes=new Map(),types=[{value:'IoT',checked:true},{value:'AI 安全',checked:true},{value:'Pwn',checked:false}],calls=[];
function $(id){if(!nodes.has(id))nodes.set(id,{value:'',disabled:false,focus(){}});return nodes.get(id);}
$('#c-mode').value='ctf';$('#c-ctf-type-note').value=' 固件协议与提示词注入 ';
let fail=true,oldBackend=false;const notices=[];
const ctx=vm.createContext({$,S:{homeFiles:[]},document:{querySelectorAll:sel=>sel.includes(':checked')?types.filter(t=>t.checked):types,querySelector:()=>null},
  toast:message=>notices.push(message),openSession(){},refreshSessions(){},renderFileChips(){},openRuleConflict(){},stageFiles:async()=>[],
  api:async(url,opts)=>{calls.push(JSON.parse(opts.body));if(fail){fail=false;throw new Error('fixture failure');}
    if(oldBackend)throw Object.assign(new Error('unsupported'),{detail:[{type:'extra_forbidden',loc:['body','ctf_type_hints']}]});
    return {session_id:'fixture'};}});
vm.runInContext(hints+launch,ctx);
(async()=>{
 assert.equal(JSON.stringify(ctx.ctfTypeHintPayload()),JSON.stringify({ctf_type_hints:['IoT','AI 安全'],ctf_type_note:'固件协议与提示词注入'}));
 await ctx.launchMission({mission:'附件题'});
 assert.equal(types[0].checked,true,'请求失败保留选项');assert.match($('#c-ctf-type-note').value,/固件/);
 await ctx.launchMission({mission:'附件题'});
 assert.deepEqual(calls[1].ctf_type_hints,['IoT','AI 安全']);assert.equal(calls[1].ctf_type_note,'固件协议与提示词注入');
 assert.ok(types.every(t=>!t.checked),'成功后清空本次选项');assert.equal($('#c-ctf-type-note').value,'');
 $('#c-ctf-type-note').value='其他类型';
 assert.equal(JSON.stringify(ctx.ctfTypeHintPayload()),JSON.stringify({ctf_type_hints:[],ctf_type_note:'其他类型'}),'只填写补充类型也有效');
 types[0].checked=true;await ctx.launchMission({mode:'src',mission:'本地审计'});
 assert.equal('ctf_type_hints' in calls.at(-1),false,'SRC 不发送隐藏的题型草稿');
 assert.equal(types[0].checked,true,'切换模式保留未发送的 CTF 草稿');
 types.forEach(t=>t.checked=false);$('#c-ctf-type-note').value='';
 assert.equal(JSON.stringify(ctx.ctfTypeHintPayload()),'{}','空选沿用自动判断');
 types[0].checked=true;oldBackend=true;
 const before=calls.length;await ctx.launchMission({mode:'ctf',mission:'hinted challenge'});
 assert.equal(calls.length,before+1,'旧后端不静默丢掉提示重发任务');
 assert.match(notices.at(-1),/后端尚未加载题型提示/);assert.equal(types[0].checked,true);
 console.log('CTF type hints: multiple choices, custom-only, retry, reset, mode isolation and deployment notice passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
