"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm"),path=require("node:path");
class Element {
  get innerHTML(){return this._html || "";}
  set innerHTML(value){this._html=value;this.nodes?.clear();this.groups={};}
  constructor(){this.innerHTML="";this.textContent="";this.nodes=new Map();this.dataset={};this.attrs={};this.classList={toggle:(key,value)=>this.attrs[key]=value};}
  querySelector(selector){if(!this.nodes.has(selector))this.nodes.set(selector,new Element());return this.nodes.get(selector);}
  querySelectorAll(selector){
    const attr=selector==="[data-packet]"?"packet":selector==="[data-packet-row]"?"packet-row":null;
    if(!attr)return [];
    this.groups ||= {};
    const key=attr+this.innerHTML;
    if(!this.groups[key]) this.groups[key]=[...this.innerHTML.matchAll(new RegExp(`data-${attr}="(\\d+)"`,"g"))].map(match=>{
      const el=new Element();el.dataset[attr==="packet"?"packet":"packetRow"]=match[1];return el;
    });
    return this.groups[key];
  }
  replaceChildren(){this.innerHTML="";this.textContent="";} setAttribute(k,v){this.attrs[k]=v;} removeAttribute(){} scrollIntoView(){}
}
const pending=[], context=vm.createContext(require('./helpers/i18n').context({URLSearchParams,
  esc:s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])),
  api:path=>new Promise(resolve=>pending.push({path,resolve}))}));
vm.runInContext(fs.readFileSync(path.join(__dirname,"../src/spiritscout/web/static/js/packet-browser.js"),"utf8"),context);
const run=code=>vm.runInContext(code,context),flush=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
const capture={capture_id:"a".repeat(32),name:"fixture.pcap",packet_count:2,status:"completed"};
const packet={id:1,number:1,relative_time:0.123456,source:"10.0.0.1",destination:"10.0.0.2",source_port:80,destination_port:50000,protocol:"HTTP",length:64,info:"<img onerror=bad>",time:1,capture_id:capture.capture_id,tcp_stream:null,udp_stream:null,layers:{http:{header:"<script>"}},raw_hex:"00413c3e",payload_text:"GET /chat HTTP/1.1"};
(async()=>{
  assert.match(run('PacketBrowser.hexDump("00413c3e")'),/00000000  00 41 3c 3e/);
  assert.match(run('PacketBrowser.treeHtml({http:{value:"<script>"}})'),/&lt;script&gt;/);
  const root=new Element();context.root=root;
  run('var browser=PacketBrowser.mount(root,"first","' + capture.capture_id + '");browser.refresh()');
  assert.doesNotMatch(root.innerHTML,/开始采集|data-upload|BPF/);
  pending[0].resolve({items:[capture,{...capture,capture_id:"b".repeat(32)}]});await flush();
  assert.match(pending[1].path,/order=asc/);
  pending[1].resolve({items:[packet,{...packet,id:2,number:2}],has_more:false});await flush();
  const list=root.querySelector("[data-packet-list]");assert.match(list.innerHTML,/&lt;img onerror=bad&gt;/);
  pending[2].resolve(packet);await flush();
  assert.equal(root.querySelector("[data-packet-payload]").textContent,"GET /chat HTTP/1.1");
  assert.match(root.querySelector("[data-packet-hex]").textContent,/\.A<>/);
  assert.equal(list.querySelectorAll("[data-packet-row]")[0].attrs["aria-selected"],"true");
  list.querySelectorAll("[data-packet]")[1].onclick();const late=pending[3];
  run('browser.close();var second=PacketBrowser.mount(root,"second");second.refresh()');
  late.resolve({...packet,id:2});await flush();
  assert.equal(root.querySelector("[data-protocol-tree]").innerHTML,"");
  pending[4].resolve({items:[]});await flush();
  assert.match(root.querySelector("[data-packet-message]").textContent,/尚无可显示/);
  run('second.close()');
  console.log("offline packets: attachment isolation, timeline, payload, hex, selection and stale details passed");
})().catch(error=>{console.error(error);process.exitCode=1;});
