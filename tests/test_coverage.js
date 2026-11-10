'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
function box(){return {isConnected:true,innerHTML:'',querySelector(){return {onclick:null};},querySelectorAll(){return [];}};}
const requests=[],workspace={replaceChildren(){},hidden:false};
const context=vm.createContext(require('./helpers/i18n').context({
 S:{cur:'sess-a',tab:'assets',view:'session'},AbortController,URLSearchParams,
 esc:value=>String(value??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])),
 document:{hidden:false,addEventListener(){},querySelector:()=>workspace,querySelectorAll:()=>[]},
 api:(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject})),
 setTimeout:()=>0,clearTimeout(){},
}));
vm.runInContext(fs.readFileSync('src/spiritscout/web/static/js/coverage.js','utf8'),context);
const coverage=vm.runInContext('CoveragePage',context);
const row={ref:'topology:test@1',identity:'member-a',test_type:'权限对照',status:'pending',reason:'<script>untrusted</script>',text:'record',evidence:[]};
const data={total:1,items:[row]};
const record=id=>({id,layer:'asset',record_id:id,title:id});
(async()=>{
 const firstBox=box(),secondBox=box(),panel={_curId:'a',box:firstBox,querySelector(){return this.box;}};
 const first=coverage.assetSummary(record('a'),panel);
 panel._curId='b';panel.box=secondBox;
 const second=coverage.assetSummary(record('b'),panel);
 assert.equal(new URLSearchParams(requests[1].url.split('?')[1]).get('asset_ref'),'asset:b');
 assert.equal(new URLSearchParams(requests[1].url.split('?')[1]).get('include_unattempted'),'false');
 assert.ok(requests[0].options.signal.aborted);
 requests[1].resolve(data);await second;
 assert.match(secondBox.innerHTML,/member-a/);assert.match(secondBox.innerHTML,/&lt;script&gt;/);
 const latest=secondBox.innerHTML;
 requests[0].resolve({total:0,items:[]});await first;
 assert.equal(secondBox.innerHTML,latest,'迟到的旧资产结果不能覆盖当前详情');
 assert.equal(firstBox.innerHTML,'','旧详情也不应被迟到请求重写');
 const stale=coverage.assetSummary(record('b'),panel);context.S.cur='sess-b';
 requests[2].reject(new Error('old session error'));await stale;
 assert.equal(secondBox.innerHTML,latest,'旧会话失败不能覆盖新会话内容');
 const empty=coverage.assetSummary(record('b'),panel);requests[3].resolve({total:0,items:[]});await empty;
 assert.match(secondBox.innerHTML,/尚无关联测试记录/);
 assert.doesNotMatch(secondBox.innerHTML,/未见尝试/,'缺少关联不应表示没有执行过请求');
 const leaving=coverage.assetSummary(record('b'),panel);context.S.view='home';coverage.navigate();
 const before=secondBox.innerHTML;requests[4].resolve(data);await leaving;
 assert.equal(secondBox.innerHTML,before,'离开页面后不应用迟到结果');
 console.log('asset tests: exact references, honest empty state, escaped text, selection/session/navigation race isolation passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
