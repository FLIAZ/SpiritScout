"use strict";
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
function element() {
  return {hidden:false, disabled:false, textContent:'', innerHTML:'', title:'', dataset:{}, open:false,
    classList:{toggle(){}}, setAttribute(){}, addEventListener(){},
    showModal(){this.open=true;},close(){this.open=false;},querySelectorAll(){return [element(),element(),element(),element()];}};
}
async function fixture() {
  const nodes = new Map(), calls = [], timers = [];
  const node = key => {if (!nodes.has(key)) nodes.set(key,element()); return nodes.get(key);};
  const state = {current:'0.20.0',installation:{kind:'source',label:'Git',supported:true},available:true,can_update:true,
    release:{tag:'v0.20.1',commit:'abc',changelog_source:'CHANGELOG.md',changelog:'# release\n<script>alert(1)</script>'},job:{}};
  let fail = false;
  const context = vm.createContext(require('./helpers/i18n').context({document:{visibilityState:'visible'}, location:{reload(){}},
    $:node,esc:s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])),
    setTimeout:fn=>{timers.push(fn);return timers.length;},clearTimeout(){},
    api:async(path,opt={})=>{
      calls.push([path,opt]);
      if (path.endsWith('/apply')) {
        if(fail)throw new Error('源码已变化');
        state.job={phase:'preparing',message:'准备中'};state.can_update=false;return {ok:true};
      }
      const value=JSON.parse(JSON.stringify(state));
      if(!path.includes('details=true')&&!path.endsWith('/check'))delete value.release.changelog;
      return value;
    }}));
  vm.runInContext(fs.readFileSync('src/spiritscout/web/static/js/markdown.js','utf8'),context);
  vm.runInContext(fs.readFileSync('src/spiritscout/web/static/js/update-guides.js','utf8'),context);
  vm.runInContext(fs.readFileSync('src/spiritscout/web/static/js/updates.js','utf8'),context);
  await new Promise(resolve=>setImmediate(resolve));
  return {node,state,calls,setFail:()=>{fail=true;}};
}
(async()=>{
  const view=await fixture();
  assert.equal(view.node('#update-dlg').open,false,'新版提醒不能自动打开弹层');
  assert.equal(view.node('#btn-update').hidden,false);
  assert.equal(view.calls[0][0],'/api/updates','后台状态轮询不传输整份日志');
  await view.node('#btn-update').onclick();
  assert.equal(view.node('#update-dlg').open,true);
  assert.equal(view.calls.at(-1)[0],'/api/updates?details=true');
  assert.match(view.node('#update-changelog').innerHTML,/&lt;script&gt;/);
  await view.node('#update-apply').onclick();
  const apply=view.calls.find(([path])=>path.endsWith('/apply'));
  assert.deepEqual(JSON.parse(apply[1].body),{tag:'v0.20.1'});
  assert.equal(view.node('#update-apply').disabled,true);
  assert.equal(view.node('#update-steps').hidden,false);
  const failure=await fixture();
  await failure.node('#btn-update').onclick();failure.setFail();
  await failure.node('#update-apply').onclick();
  assert.equal(failure.node('#update-status').textContent,'源码已变化','刷新状态不能吞掉失败原因');
  console.log('updates: quiet reminder, on-demand safe changelog, explicit apply and failure feedback passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
