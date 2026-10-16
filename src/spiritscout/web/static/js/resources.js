/* 资源管理对话拥有独立历史；目录、检索和变更不会派发安全测试。 */
const ResourceConsole = (() => {
  const states = new Map(); let editor = null, poll;
  const titles = {mcp:['工具接入','MCP 管理','接入工具，把每一次配置看清楚。'],skill:['参考知识','Skill 管理','留下可复用的知识，让方法保持开放。'],assets:['持续观测','资产收集','同一资产，一条有来源的时间线。']};
  const stateFor = kind => {if(!states.has(kind)) states.set(kind,{items:[],offset:0,q:'',generation:0});return states.get(kind);};
  const root = kind => $(`#view-${kind}`);
  const time = t => t ? new Date(t*1000).toLocaleString('zh-CN',{hour12:false}) : '未记录';
  function shell(kind) {
    const [eyebrow,title,description]=titles[kind],el=root(kind);if(el.dataset.ready)return;el.dataset.ready='yes';
    el.innerHTML=`<header class="resource-head"><div><span class="resource-eyebrow">${eyebrow}</span><h2>${title}</h2><p>${description}</p></div><div class="resource-head-actions"><button class="btn" data-refresh>刷新</button><button class="btn primary" data-new>${kind==='assets'?'同步历史会话':'手动添加'}</button></div></header>
      <div class="resource-layout ${kind==='assets'?'resource-assets-layout':''}"><section class="resource-catalog" aria-label="${title}目录"><label class="resource-search">${kind==='assets'?'按域名、地址或类型检索':'检索已配置资源'}<input type="search" data-search placeholder="输入关键词…" maxlength="200"></label><div class="resource-list-status" role="status"></div><div class="resource-list"></div><button class="btn resource-more" hidden>加载更多</button></section>
      ${kind==='assets'?'<aside class="resource-asset-detail"><div class="resource-empty">选择资产，查看观测来源与变化。<p>历史结果不携带授权，新的任务仍需复核。</p></div></aside>':`<section class="resource-chat" aria-label="${title}专属管理对话"><header><span class="resource-dot"></span><div><b>资源助手</b><small>独立对话 · 先查看变更，再确认执行</small></div></header><div class="resource-chat-log" aria-live="polite"></div><div class="resource-proposals"></div><form class="resource-chat-form"><label class="sr-only" for="resource-message-${kind}">向${title}助手描述需求</label><textarea id="resource-message-${kind}" placeholder="${kind==='mcp'?'例如：帮我配置一个只读浏览器 MCP，请先给出安装方案':'例如：添加一份关于接口权限边界的参考技能'}" rows="3" maxlength="16000"></textarea><div><small>请勿在聊天中粘贴密钥</small><button class="btn primary" type="submit">发送</button></div><p class="resource-chat-status" role="status"></p></form></section>`}</div>`;
    el.querySelector('[data-refresh]').onclick=()=>{load(kind);if(kind!=='assets')loadChat(kind);};
    el.querySelector('[data-new]').onclick=async()=>{if(kind==='assets'){const button=el.querySelector('[data-new]');button.disabled=true;try{const r=await api('/api/assets/sync',{method:'POST',body:'{}'});toast(`已扫描 ${r.scanned_records} 条资产${r.failed_sessions?`，${r.failed_sessions} 个会话需重试`:''}`);load(kind);}catch(e){toast(e.message,true);}finally{button.disabled=false;}}else edit(kind);};
    let timer;el.querySelector('[data-search]').oninput=e=>{const s=stateFor(kind);s.q=e.target.value.trim();s.generation++;clearTimeout(timer);timer=setTimeout(()=>load(kind),250);};
    el.querySelector('.resource-more').onclick=()=>load(kind,true);
    if(kind!=='assets')el.querySelector('form').onsubmit=e=>send(kind,e);
  }
  async function load(kind,append=false) {
    const s=stateFor(kind),version=++s.generation,status=root(kind).querySelector('.resource-list-status');
    status.textContent='正在读取…';
    try{
      const offset=append?s.items.length:0;
      const path=kind==='assets'?'/api/assets':`/api/resources/${kind==='skill'?'skills':'mcp'}`;
      const result=await api(path+'?'+new URLSearchParams({q:s.q,offset:String(offset),limit:'40'}));
      if(version!==s.generation)return;
      let rows=result.items||[];if(kind==='mcp'&&s.q)rows=rows.filter(x=>JSON.stringify(x.data).toLowerCase().includes(s.q.toLowerCase()));
      s.items=append?[...s.items,...rows]:rows;s.more=result.has_more;s.total=result.total??rows.length;
      status.textContent=`${s.total} 条记录${kind==='mcp'?' · 保存后新会话生效':kind==='assets'?' · 历史参考，本次仍需复核':''}`;render(kind);
    }catch(e){status.textContent=`读取失败：${e.message}。点击刷新重试。`;}
  }
  function render(kind) {
    const s=stateFor(kind),list=root(kind).querySelector('.resource-list');
    list.innerHTML=s.items.map((item,i)=>{
      const d=kind==='mcp'?item.data:item;
      const title=kind==='assets'?d.data.value:kind==='mcp'?d.name:d.skill_id;
      const badge=kind==='assets'?(d.changed?'观测有变化':d.freshness==='recent'?'近期观测':'建议复核'):(d.enabled?'已启用':'已停用');
      const subtitle=kind==='assets'?`${({endpoint:'接口',app:'应用页面',url:'网址',domain:'域名',host:'主机',ip:'IP',service:'服务端口',finding:'历史发现'})[d.kind]||d.kind} · ${d.observations} 次观测 · ${time(d.last_seen)}`:kind==='mcp'?`${d.transport} · ${d.command||d.url||'尚未配置地址'}`:d.description||'尚无描述';
      return `<article class="resource-item"><button class="resource-select" data-select="${i}"><span class="resource-badge ${kind==='assets'&&d.changed?'changed':''}">${esc(badge)}</span><strong>${esc(title)}</strong><p>${esc(subtitle)}</p><small>${esc(kind==='assets'?d.source:kind==='mcp'?`配置版本 ${item.version} · ${d.default_risk} · ${d.allowed_agents?.length?d.allowed_agents.join(', '):'沿用角色默认范围'}`:`${d.domain||'未分类'} · ${d.origin==='builtin'?'内置参考':'自定义'} · v${d.version}`)}</small></button>${kind==='assets'?'':`<button class="btn sm resource-toggle" data-toggle="${i}">${d.enabled?'停用':'启用'}</button>`}</article>`;
    }).join('')||`<div class="resource-empty">还没有匹配记录<p>${kind==='assets'?'可更换关键词，或同步历史会话中的资产。':'可更换关键词，或从资源助手对话开始配置。'}</p></div>`;
    list.querySelectorAll('[data-select]').forEach(b=>b.onclick=()=>kind==='assets'?assetDetail(s.items[+b.dataset.select]):edit(kind,s.items[+b.dataset.select]));
    list.querySelectorAll('[data-toggle]').forEach(b=>b.onclick=async()=>{const item=s.items[+b.dataset.toggle],d=kind==='mcp'?item.data:item;b.disabled=true;try{await api(`/api/resources/${kind==='mcp'?'mcp':'skills'}/${encodeURIComponent(kind==='mcp'?item.id:d.skill_id)}`,{method:'PUT',body:JSON.stringify({data:{enabled:!d.enabled},expected_version:item.version??d.version})});load(kind);}catch(e){toast(e.message,true);b.disabled=false;}});
    root(kind).querySelector('.resource-more').hidden=!s.more;
  }
  async function assetDetail(item) {
    const el=root('assets').querySelector('.resource-asset-detail');el.innerHTML='<p class="resource-empty">正在读取观测历史…</p>';const version=(stateFor('assets').detailGeneration||0)+1;stateFor('assets').detailGeneration=version;
    try{const history=await api(`/api/assets/${encodeURIComponent(item.id)}/history`);if(stateFor('assets').detailGeneration!==version)return;
      el.innerHTML=`<div class="asset-detail-head"><span class="resource-eyebrow">观测档案</span><h3>${esc(item.data.value)}</h3><p>最近观测 ${esc(time(item.last_seen))}</p><p class="resource-history-note">这些是历史记录。资产、账号权限和内容可能变化，不能视为本次已验证。</p><button class="btn" data-recheck>用新会话复核此资产 ↗</button></div><div class="asset-observations">${history.items.map((entry,i)=>`<article><span class="asset-time">${esc(time(entry.observed_at))}${i===0?' · 最近观测':''}</span><small>${esc(entry.source)}</small><pre>${esc(JSON.stringify(entry.data,null,2))}</pre></article>`).join('')}</div>${history.has_more?'<p class="muted">当前显示最近 30 条观测。</p>':''}`;
      el.querySelector('[data-recheck]').onclick=()=>{show('home');$('#c-mission').value=`复核 ${item.data.value} 的当前状态。先读取已有资产观测，对比变化；历史结果仅供参考。请依据本次提供的授权开展。`;$('#c-mission').focus();};
    }catch(e){el.textContent=`历史读取失败：${e.message}`;}
  }
  async function edit(kind,item) {
    let data=kind==='mcp'?item?.data:null;
    if(kind==='skill'&&item){try{data=await api('/api/resources/skills/'+encodeURIComponent(item.skill_id));}catch(e){toast(e.message,true);return;}}
    editor={kind,version:item?.version||0};$('#resource-editor-title').textContent=kind==='mcp'?'MCP 接入配置':'Skill 参考知识';$('#resource-key').value=kind==='mcp'?item?.id||'':item?.skill_id||'';$('#resource-key').disabled=!!item;
    const clean=Object.fromEntries(Object.entries(data||{}).filter(([key])=>!['id','version','origin','env_keys','headers_keys','name','skill_id'].includes(key)));
    $('#resource-json').value=JSON.stringify(data?clean:kind==='mcp'?{transport:'stdio',command:'',args:[],enabled:true,default_risk:'intrusive',risk_by_action:false,allowed_agents:[]}:{description:'',domain:'custom',body:'适用条件：\n参考事实：\n证据与注意事项：',enabled:true},null,2);
    $('#resource-env').value='';$('#resource-headers').value='';$('#resource-secrets').hidden=kind!=='mcp';$('#resource-save-error').textContent='';$('#resource-editor').showModal();
  }
  async function save() {if(!editor)return;const button=$('#resource-save');button.disabled=true;try{const data=JSON.parse($('#resource-json').value),key=$('#resource-key').value.trim();if(!key)throw new Error('请填写唯一标识');if(editor.kind==='mcp'){if($('#resource-env').value.trim())data.env=JSON.parse($('#resource-env').value);if($('#resource-headers').value.trim())data.headers=JSON.parse($('#resource-headers').value);}await api(`/api/resources/${editor.kind==='mcp'?'mcp':'skills'}/${encodeURIComponent(key)}`,{method:'PUT',body:JSON.stringify({data,expected_version:editor.version})});$('#resource-editor').close();toast('配置已保存');load(editor.kind);}catch(e){$('#resource-save-error').textContent=e.message;}finally{button.disabled=false;}}
  function renderChat(kind,data) {
    const el=root(kind),log=el.querySelector('.resource-chat-log'),signature=JSON.stringify(data);if(el.dataset.chatSignature===signature)return;el.dataset.chatSignature=signature;
    const follow=log.scrollHeight-log.scrollTop-log.clientHeight<40;
    const usage=data.usage_total;if(usage)el.querySelector('.resource-chat header small').textContent=`独立对话 · 累计 ${Number(usage.used_tokens||0).toLocaleString('zh-CN')} token · ${usage.calls||0} 次调用`;
    log.innerHTML=data.messages.map(m=>`<div class="resource-message ${m.role==='user'?'user':'assistant'}"><small>${m.role==='user'?'你':'资源助手'} · ${esc(time(m.created_at))}</small><p>${esc(m.content)}</p></div>`).join('')||'<div class="resource-chat-welcome"><span>先描述需求</span><h3>把接入配置，交给对话。</h3><p>告诉我你想接入的工具或参考技能。我会整理待确认变更，并询问缺少的信息。</p></div>';
    if(follow)log.scrollTop=log.scrollHeight;
    const proposals=el.querySelector('.resource-proposals');proposals.innerHTML=(data.proposals||[]).map(p=>`<details class="resource-proposal" ${['pending','applying'].includes(p.state)?'open':''}><summary>${esc(({pending:'待确认变更',applying:'正在安装与配置',completed:'变更已完成',failed:'执行失败',interrupted:'执行已中断'})[p.state]||p.state)}</summary><pre>${esc(JSON.stringify(p.proposal,null,2))}</pre>${p.result?`<p>${esc(p.result)}</p>`:''}${['pending','failed','interrupted'].includes(p.state)?`<button class="btn primary" data-apply="${esc(p.id)}">查看后应用此变更</button>`:''}</details>`).join('');
    proposals.querySelectorAll('[data-apply]').forEach(b=>b.onclick=async()=>{if(!b.dataset.confirmed){b.dataset.confirmed='yes';b.textContent='确认执行上述安装与配置';return;}b.disabled=true;try{await api(`/api/resources/proposals/${b.dataset.apply}/apply`,{method:'POST',body:JSON.stringify({confirm:true})});loadChat(kind);}catch(e){toast(e.message,true);b.disabled=false;}});
    if((data.proposals||[]).some(p=>p.state==='completed'))load(kind);
  }
  async function loadChat(kind) {try{const data=await api(`/api/resources/${kind}/chat`);if(S.view===kind)renderChat(kind,data);}catch(e){root(kind).querySelector('.resource-chat-status').textContent=`管理对话读取失败：${e.message}`;}}
  async function send(kind,event) {event.preventDefault();const form=event.currentTarget,input=form.querySelector('textarea'),text=input.value.trim();if(!text)return;const button=form.querySelector('button');button.disabled=true;const status=form.querySelector('.resource-chat-status');status.textContent='资源助手正在整理需求…';try{const result=await api(`/api/resources/${kind}/chat`,{method:'POST',body:JSON.stringify({message:text})});if(input.value.trim()===text)input.value='';if(S.view===kind)renderChat(kind,result);status.textContent=result.usage?`本次 ${result.usage.input_tokens+result.usage.output_tokens} token · ${result.usage.model}`:'';}catch(e){status.textContent=`处理失败：${e.message}。输入已保留，可重试。`;loadChat(kind);}finally{button.disabled=false;}}
  function open(kind) {clearInterval(poll);shell(kind);load(kind);if(kind!=='assets'){loadChat(kind);poll=setInterval(()=>{if(S.view!==kind){clearInterval(poll);return;}loadChat(kind);},2500);}}
  document.querySelectorAll('[data-resource]').forEach(button=>button.onclick=()=>{S.cur=null;show(button.dataset.resource);renderSessions();});
  $('#resource-save').onclick=save;
  $('#resource-editor').addEventListener('close',()=>{$('#resource-env').value='';$('#resource-headers').value='';});
  return {open};
})();
