"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('src/spiritscout/web/static/js/console.js','utf8');
const sendSource=source.slice(source.indexOf('const composerDeliveries'),source.indexOf('/* ================= 发起会话'));
const file={name:'evidence.txt'}, input={value:'分析附件'}, pending=new Set();
const S={cur:'sess-a',sessions:[{session_id:'sess-a',status:'running'}],compFiles:[file]};
let stages=0, fail=true, edit=false, requests=[];
const ctx=vm.createContext(require('./helpers/i18n').context({S,compInput:input,sessionMessagePending:pending,
  syncComposerControl(){},compGrow(){},renderFileChips(){},toast(){},
  startStreams(){},refreshState(){},refreshSessions(){},
  stageFiles:async files=>{stages++;return files.map((_,i)=>`upload-${i}`);},
  api:async(url,opts)=>{
    requests.push({url,body:JSON.parse(opts.body)});
    if(fail){fail=false;throw new Error('响应丢失');}
    if(edit)input.value='下一条草稿';
    return {queued:true,resumed:false};
  },
}));
vm.runInContext(sendSource,ctx);
(async()=>{
  await ctx.compSend();
  assert.equal(S.compFiles.length,1,'失败不丢附件草稿');
  await ctx.compSend();
  assert.equal(stages,1,'同一次发送失败重试不重复暂存文件');
  assert.equal(requests.length,2);
  assert.equal(requests[0].body.message_id,requests[1].body.message_id,'重试沿用稳定消息ID');
  assert.deepEqual(requests[1].body.upload_ids,['upload-0']);
  assert.equal(requests[1].body.content,'分析附件');
  assert.ok(requests.every(item=>item.url.endsWith('/messages')),'正文和文件使用一个消息端点');
  assert.equal(S.compFiles.length,0);
  input.value='';S.compFiles=[file];
  await ctx.compSend();
  assert.equal(requests.at(-1).body.content,'','仅附件也提交消息信封');
  assert.deepEqual(requests.at(-1).body.upload_ids,['upload-0']);
  input.value='原消息';S.compFiles=[];edit=true;
  await ctx.compSend();
  assert.equal(input.value,'下一条草稿','响应期间用户编辑的新草稿不被清空');
  assert.equal(pending.size,0);
  console.log('composer delivery: atomic envelope, stable retries, attachment-only and draft isolation passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
