"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
const handlers={},classes=new Set(),hint={hidden:true},zone={contains:node=>!!node?.inside,
  classList:{remove:key=>classes.delete(key),toggle:(key,on)=>on?classes.add(key):classes.delete(key)}};
const homeClasses=new Set(),homeHint={hidden:true},homeZone={contains:node=>!!node?.home,
  classList:{remove:key=>homeClasses.delete(key),add:key=>homeClasses.add(key)}};
zone.classList.add=key=>classes.add(key);
const send={disabled:false};
const S={cur:"one",view:"session",compFiles:[],homeFiles:[]},pending=new Set(),messages=[];
let renders=0,modal=false;
const context=vm.createContext(require('./helpers/i18n').context({S,sessionMessagePending:pending,
  toast:message=>messages.push(message),renderFileChips:(selector,files)=>{assert.equal(selector,S.view==="home"?"#home-files":"#comp-files");assert.equal(files,S.view==="home"?S.homeFiles:S.compFiles);renders++;},
  document:{getElementById:id=>({"view-session":zone,"view-home":homeZone,"comp-drop-status":hint,"home-drop-status":homeHint,"c-go":send})[id],querySelector:()=>modal?{}:null,addEventListener:(type,handler)=>handlers[type]=handler},
  ComposerFolders:{collect:async transfer=>Array.from(transfer.files)},
  window:{addEventListener:(type,handler)=>handlers[type]=handler}}));
vm.runInContext(fs.readFileSync("src/spiritscout/web/static/js/composer-drop.js","utf8"),context);
function event(files=[{name:"evidence.txt",size:3}],inside=true){return{target:{inside},dataTransfer:{types:["Files"],files,items:[]},preventDefault(){this.prevented=true;}};}
(async()=>{
let e=event();handlers.dragover(e);assert.equal(e.prevented,true);assert.equal(e.dataTransfer.dropEffect,"copy");assert.equal(hint.hidden,false);
await handlers.drop(e);assert.equal(S.compFiles.length,1);assert.equal(renders,1);assert.equal(hint.hidden,true);
S.compFiles=[];await handlers.drop(event([{name:"second.txt",size:2}]));assert.equal(S.compFiles[0].name,"second.txt","切换草稿后使用当前附件数组");
pending.add("one");await handlers.drop(event());assert.equal(S.compFiles.length,1);assert.match(messages.at(-1),/正在处理/);pending.clear();
await handlers.drop(event([{name:"empty.txt",size:0}]));assert.equal(S.compFiles.length,1);
await handlers.drop(event([{name:"large.zip",size:50*1024*1024+1}]));assert.equal(S.compFiles.length,1);
await handlers.drop(event(Array.from({length:20},()=>({name:"x",size:1}))));assert.equal(S.compFiles.length,1);assert.match(messages.at(-1),/20/);
e=event([],false);await handlers.drop(e);assert.equal(e.prevented,true);assert.equal(renders,2);
modal=true;await handlers.drop(event());assert.equal(renders,2);modal=false;
S.view="home";await handlers.drop(event());assert.equal(renders,2);
e=event([{name:"challenge",size:13}]);e.target={home:true};
handlers.dragover(e);assert.equal(homeHint.hidden,false);
await handlers.drop(e);assert.equal(S.homeFiles[0].name,"challenge");assert.equal(S.compFiles.length,1);
send.disabled=true;await handlers.drop(e);assert.equal(S.homeFiles.length,1);send.disabled=false;
S.homeFiles=[];await handlers.drop(e);assert.equal(S.homeFiles.length,1,"主页使用当前附件数组");
let release;context.ComposerFolders.collect=()=>new Promise(resolve=>{release=resolve;});
const reading=handlers.drop(e);S.view="session";release([{name:"late",size:1}]);await reading;
assert.equal(S.homeFiles.length,1);assert.equal(S.compFiles.length,1,"切页后不串附件");
e={dataTransfer:{types:["text/plain"]},preventDefault(){throw new Error("不能拦截普通文本拖动");}};handlers.dragover(e);await handlers.drop(e);
console.log("composer drop: current drafts, file-only, pending sends, navigation prevention and validation passed");

})().catch(error=>{console.error(error);process.exitCode=1;});
