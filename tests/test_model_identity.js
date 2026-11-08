'use strict';
const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const code=fs.readFileSync('src/spiritscout/web/static/js/models.js','utf8');
const nodes=new Map();const $=id=>{if(!nodes.has(id))nodes.set(id,{value:'',textContent:'',innerHTML:'',querySelectorAll:()=>[],showModal(){},addEventListener(){}});return nodes.get(id);};
const a={model:'shared/model',key:'@provider-a-key',provider_ref:'A',context_window:100};
const b={model:'shared/model',key:'@provider-b-key',provider_ref:'B',context_window:200};
const errors=[];
const ctx=vm.createContext(require('./helpers/i18n').context({$,S:{models:{models:[a,b]}},esc:String,
  modelDisplayName:String,selxSync(){},toast:(message,error)=>{if(error)errors.push(message);},
  ProviderAccounts:{modelUsage:()=>'',quotaSlot:()=>'',controls:()=>'',reset(){},refreshVisible(){}}}));
ctx.window=ctx;
vm.runInContext(code,ctx);
ctx.fillPrefs=v=>ctx.pref=v;
assert.match(ctx.modelTile(a),/data-model="@provider-a-key"/);
assert.match(ctx.modelTile(b),/data-model="@provider-b-key"/);
ctx.window.modelEdit(b.key);
assert.equal(ctx.S.medit,b.key);assert.equal($('#m-ctx').value,200);assert.equal(ctx.pref,'B');
ctx.window.modelEdit(a.key);
assert.equal($('#m-ctx').value,100);assert.equal(ctx.pref,'A');

async function rendering() {
  // 另一供应商的同名模型不能被误标为已启用。
  ctx.S.models.models=[a];
  ctx.api=async()=>({ok:true,url:'local-fixture',models:[{id:a.model}]});
  await ctx.providerFetch('B');
  const checkbox=$('#fm-body').innerHTML.match(/<input\b[^>]*>/)[0];
  assert.match(checkbox,/\bchecked\b/);assert.doesNotMatch(checkbox,/\bdisabled\b/);

  // 观察整个渲染路径的数据访问量，防止供应商数量乘以全量模型遍历。
  let reads=0;
  const providers=Array.from({length:20},(_,i)=>({name:`provider-${i}`,provider:'mock'}));
  const models=Array.from({length:400},(_,i)=>({key:`key-${i}`,model:`model-${i}`,
    get provider_ref(){reads++;return providers[i%providers.length].name;}}));
  let snapshot={models,path:'fixture'};
  let chain=['key-5','missing'];
  ctx.loadPresets=async()=>[];
  ctx.api=async url=>url==='/api/providers'?{providers}:url==='/api/models'?snapshot:{chain};
  await ctx.loadModels();
  assert.deepEqual(errors,[]);
  assert.ok(reads<=models.length*4,`渲染应按模型数量线性读取，实际 ${reads} 次`);
  assert.equal(($('#providers-list').innerHTML.match(/class="mtile model-row"/g)||[]).length,models.length);
  assert.match($('#naming-chain').innerHTML,/model-5/);
  assert.match($('#naming-chain').innerHTML,/missing/);

  snapshot={models:[{key:'key-5',model:'renamed-model',provider_ref:'provider-1'}],path:'next'};
  chain=['key-5'];
  await ctx.loadModels();
  assert.match($('#naming-chain').innerHTML,/renamed-model/);
  assert.match($('#providers-list').innerHTML,/renamed-model/);
  assert.doesNotMatch($('#providers-list').innerHTML,/>model-5</);
  const first=ctx.indexModels([a,{...a,model:'duplicate'}]);
  assert.equal(first.byKey.get(a.key),a,'重复身份保留原 find 的首项语义');
  assert.deepEqual(errors,[]);
  console.log('models: provider identity, fetch selection, linear grouping and refreshed naming passed');
}

const core=fs.readFileSync('src/spiritscout/web/static/js/core.js','utf8');
const labels=vm.createContext(require('./helpers/i18n').context({atob,TextDecoder,Uint8Array}));
vm.runInContext(core.slice(core.indexOf('function modelDisplayName('),core.indexOf('async function api(')),labels);
const display=labels.modelDisplayName;
const sample='@WyJkZWVwc2VlayIsImRlZXBzZWVrLWZsYXNoIl0';
assert.equal(display(sample),'deepseek / deepseek-flash');
const encode=(provider,model)=>'@'+Buffer.from(JSON.stringify([provider,model])).toString('base64url');
assert.equal(display(encode('供应商甲','org/模型')),'供应商甲 / org/模型');
assert.notEqual(display(encode('A','same-id')),display(encode('B','same-id')));
assert.equal(display('deepseek-flash'),'deepseek-flash');
assert.equal(display('deepseek-flash','deepseek'),'deepseek / deepseek-flash');
for(const legacy of ['@legacy-model','@!!!','@'+Buffer.from('{"not":"a pair"}').toString('base64url')])assert.equal(display(legacy),legacy);
console.log('model labels: existing encoded IDs, Unicode, same-name providers, legacy names and malformed IDs passed');
rendering().catch(error=>{console.error(error);process.exitCode=1;});
