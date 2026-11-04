"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm"),path=require("node:path");
const elements=new Map(),requests=[],created=[],revoked=[];
const element=id=>{
  if(!elements.has(id))elements.set(id,{innerHTML:"",textContent:"",open:false,handlers:{},
    addEventListener(name,fn){this.handlers[name]=fn;},showModal(){this.open=true;},
    close(){this.open=false;this.handlers.close?.();}});
  return elements.get(id);
};
const document={getElementById:element,addEventListener(){}};
const context=vm.createContext({document,window:{},AbortController,URL:{
  createObjectURL(blob){created.push(blob);return `blob:${created.length}`;},revokeObjectURL(url){revoked.push(url);}},
  mdRender:text=>text,esc:text=>String(text),toast(){},S:{},setTimeout,
  fetch(url,options){return new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}));}});
vm.runInContext(fs.readFileSync(path.join(__dirname,"../src/spiritscout/web/static/js/files.js"),"utf8"),context);
const cards=vm.runInContext("FileCards",context),body=element("fd-body"),dialog=element("file-dlg");
(async()=>{
  const a=cards.openViewer("one","artifacts/a.txt","A.txt",10);
  dialog.close();
  assert.equal(requests[0].options.signal.aborted,true);
  const b=cards.openViewer("one","artifacts/b.txt","B.txt",10);
  requests[1].resolve({ok:true,text:async()=>"B body"});await b;
  requests[0].resolve({ok:true,text:async()=>"A body"});await a;
  assert.equal(element("fd-name").textContent,"B.txt");assert.match(body.innerHTML,/B body/);
  assert.ok(!body.innerHTML.includes("A body"),"忽略取消的旧响应也不能覆盖新预览");
  const image=cards.openViewer("one","artifacts/c.png","C.png",10);
  dialog.close();requests[2].resolve({ok:true,blob:async()=>"closed-image"});await image;
  assert.equal(created.length,0,"关闭后到达的图片不能创建 Blob URL");assert.equal(body.innerHTML,"");
  const liveImage=cards.openViewer("one","artifacts/d.png","D.png",10);
  requests[3].resolve({ok:true,blob:async()=>"live-image"});await liveImage;
  assert.equal(created.length,1);
  await cards.openViewer("one","artifacts/large.txt","large.txt",5*1024*1024);
  assert.deepEqual(revoked,["blob:1"]);assert.equal(requests.length,4,"超大文件不发预览请求");
  const failed=cards.openViewer("one","artifacts/e.txt","E.txt",10);
  requests[4].reject(new Error("offline"));await failed;
  assert.match(body.innerHTML,/offline/,"当前文件的读取错误仍可见");
  console.log("file preview: stale responses, cancellation, image URL cleanup and current errors passed");
})().catch(error=>{console.error(error);process.exitCode=1;});
