"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm"),path=require("node:path");
const elements=new Map(),requests=[],created=[],revoked=[];
const packetRequests=[], packetMounts=[];
let packetCloses=0;
const element=id=>{
  if(!elements.has(id))elements.set(id,{innerHTML:"",textContent:"",open:false,handlers:{},
    addEventListener(name,fn){this.handlers[name]=fn;},showModal(){this.open=true;},
    querySelector(){return this.image || (this.image={});},
    close(){this.open=false;this.handlers.close?.();}});
  return elements.get(id);
};
const document={getElementById:element,addEventListener(){}};
const context=vm.createContext(require('./helpers/i18n').context({document,window:{},AbortController,URL:{
  createObjectURL(blob){created.push(blob);return `blob:${created.length}`;},revokeObjectURL(url){revoked.push(url);}},
  mdRender:text=>text,esc:text=>String(text),toast(){},S:{},setTimeout,clearTimeout,
  api:(url,options)=>new Promise(resolve=>packetRequests.push({url,options,resolve})),
  PacketBrowser:{mount(root,session,capture){packetMounts.push({session,capture});return {refresh:async()=>({pending:false}),close(){packetCloses++;}};}},
  fetch(url,options){return new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}));}}));
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
  context.S.cur="one";
  const snapshot={path:"artifacts/view-images/"+"b".repeat(32)+".png",name:"像素.png",size:10,sha256:"a".repeat(64)};
  assert.match(cards.imageCard(snapshot),/artifacts\/image\?path=.*sha256=/);
  assert.match(cards.imageCard(snapshot),/loading="lazy"/);
  assert.equal(cards.imageCard({...snapshot,path:"https://external.test/image.png"}),"","事件不能让浏览器加载任意远程图片");
  const snapshotView=cards.openViewer("one",snapshot.path,snapshot.name,10,snapshot.sha256);
  assert.match(requests[5].url,/artifacts\/image\?path=.*sha256=/,"放大查看使用与缩略图相同的摘要校验接口");
  requests[5].resolve({ok:true,blob:async()=>({})});await snapshotView;
  console.log("image preview: session snapshot metadata, safe URLs and digest-preserving enlargement passed");
  const beforeLocal=requests.length,localFile={name:"草稿.png",size:20};
  cards.openLocalImage(localFile);
  assert.equal(requests.length,beforeLocal,"草稿放大不上传，也不请求服务器文件接口");
  assert.equal(created.at(-1),localFile);
  const lateLocalError=body.image.onerror;
  const remoteAgain=cards.openViewer("one","artifacts/latest.txt","latest.txt",10);
  lateLocalError();assert.match(body.innerHTML,/正在读取/,"旧草稿图片的错误不能覆盖新预览");
  requests[6].resolve({ok:true,text:async()=>"最新文件"});await remoteAgain;
  cards.openLocalImage(localFile);dialog.close();
  assert.equal(body.innerHTML,"");
  assert.equal(revoked.at(-1),`blob:${created.length}`,"关闭本地放大预览后释放独立 URL");
  console.log("local image preview: no requests, stale-error isolation and dialog cleanup passed");
  assert.equal(cards.isPreviewable("offline.pcapng"),true);
  const captureCard=cards.card({rel:"attachments/offline.pcap",name:"offline.pcap",side:"user"});
  assert.match(captureCard,/点击「查看数据包」可放大查看流量包详情/);
  assert.equal(packetRequests.length,0,"附件卡片只显示提示，不自动请求解析或打开查看器");
  const staleCapture=cards.openViewer("one","attachments/old.pcap","old.pcap",10);
  dialog.close();packetRequests[0].resolve({capture_id:"old"});await staleCapture;
  assert.equal(packetMounts.length,0,"关闭后迟到解析不能重建查看器");
  context.S.cur="one";
  const manual=cards.openViewer("one","attachments/uploaded.pcap","uploaded.pcap",10);
  assert.match(packetRequests[1].url,/artifacts\/packets\?path=attachments%2Fuploaded.pcap/);
  packetRequests[1].resolve({capture_id:"current"});await manual;
  assert.equal(packetMounts[0].capture,"current");
  dialog.close();assert.equal(packetCloses,1,"关闭附件释放查看器和轮询");
  console.log("capture preview: passive attachment hint, manual opening, stale parse isolation and cleanup passed");
})().catch(error=>{console.error(error);process.exitCode=1;});
