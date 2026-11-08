"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm"),path=require("node:path");
const source=fs.readFileSync(path.join(__dirname,"../src/spiritscout/web/static/js/files.js"),"utf8");
function setup(picker, fetch) {
  const downloads=[],toasts=[],elements=new Map();
  const document={getElementById(id){if(!elements.has(id))elements.set(id,{addEventListener(){}});return elements.get(id);},addEventListener(){},
    createElement(){const anchor={click(){downloads.push({name:this.download,url:this.href});}};return anchor;}};
  class TestURL extends URL {
    static createObjectURL(){return "blob:fixture";}
    static revokeObjectURL(){}
  }
  const ctx=vm.createContext(require('./helpers/i18n').context({document,window:picker?{showSaveFilePicker:picker}:{},fetch,
    URL:TestURL,setTimeout(){},
    toast:(message,error)=>toasts.push({message,error}),esc:String,mdRender:String,S:{}}));
  vm.runInContext(source,ctx);
  return {cards:vm.runInContext("FileCards",ctx),downloads,toasts};
}
(async()=>{
  const order=[],written=[];let closed=false;
  const sink=new WritableStream({write(chunk){written.push(...chunk);},close(){closed=true;}});
  const streamed=setup(async options=>{order.push("picker");assert.equal(options.suggestedName,"fixture.txt");return{name:"chosen.txt",createWritable:async()=>sink};},
    async()=>{order.push("fetch");return{ok:true,body:new ReadableStream({start(c){c.enqueue(new TextEncoder().encode("complete file"));c.close();}}),blob(){throw new Error("stream must not be buffered");}};});
  await streamed.cards.saveFile("/file","fixture.txt");
  assert.deepEqual(order,["picker","fetch"],"必须在网络等待前调用保存位置选择器");
  assert.equal(new TextDecoder().decode(Uint8Array.from(written)),"complete file");assert.equal(closed,true);assert.equal(streamed.downloads.length,0);
  assert.match(streamed.toasts[0].message,/chosen.txt/);
  let fetches=0;
  const cancelled=setup(async()=>{throw Object.assign(new Error("cancel"),{name:"AbortError"});},async()=>{fetches++;});
  await cancelled.cards.saveFile("/file","fixture.txt");assert.equal(fetches,0);assert.equal(cancelled.downloads.length,0);
  const fallback=setup(null,async()=>({ok:true,headers:{get:()=>"attachment; filename*=UTF-8''%E4%BE%8B.txt"},blob:async()=>({})}));
  await fallback.cards.saveFile("/file");assert.equal(fallback.downloads[0].name,"例.txt");
  const unavailable=setup(async()=>{throw Object.assign(new Error("unsupported"),{name:"SecurityError"});},async()=>({ok:true,headers:{get:()=>""},blob:async()=>({})}));
  await unavailable.cards.saveFile("/file","fixture.txt");assert.equal(unavailable.downloads.length,1);
  const failed=setup(async()=>({createWritable:async()=>{throw new Error("cannot write");}}),async()=>({ok:true}));
  await failed.cards.saveFile("/file","fixture.txt");assert.equal(failed.downloads.length,0);assert.match(failed.toasts[0].message,/cannot write/);
  let inferredName="";
  const inferred=setup(async options=>{inferredName=options.suggestedName;throw Object.assign(new Error("cancel"),{name:"AbortError"});},async()=>{throw new Error("cancelled picker must not download");});
  await inferred.cards.saveFile("/api/sessions/one/artifacts/download?path=reports%2F%E6%8A%A5%E5%91%8A.md");
  assert.equal(inferredName,"报告.md");
  const report=setup(async options=>{
    assert.equal(options.suggestedName,"报告.md");
    assert.equal(options.types[0].accept["text/markdown"][0],".md");
    throw Object.assign(new Error("cancel"),{name:"AbortError"});
  },async()=>{throw new Error("cancelled picker must not download");});
  await report.cards.saveFile("/api/sessions/one/report/download");
  const reportFallback=setup(null,async()=>({ok:true,headers:{get:()=>""},blob:async()=>({})}));
  await reportFallback.cards.saveFile("/api/sessions/one/report/download");
  assert.equal(reportFallback.downloads[0].name,"报告.md");
  console.log("file download: gesture ordering, streaming, cancellation, fallback and write failures passed");
})().catch(error=>{console.error(error);process.exitCode=1;});
