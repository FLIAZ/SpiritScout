/* 资源管理对话拥有独立历史；目录、检索和变更不会派发安全测试。 */
const ResourceConsole = (() => {
  const states = new Map(); let editor = null, poll, inspecting = 0;
  const titles = {mcp:['MCP 管理','添加工具服务器，或编辑已有连接。'],skill:['Skill 管理','添加和管理智能体可查阅的参考知识。']};
  const stateFor = kind => {if(!states.has(kind)) states.set(kind,{items:[],offset:0,q:'',generation:0,filters:kind==='skill'?skillDefaults():mcpDefaults(),counts:{enabled:0,disabled:0}});return states.get(kind);};
  const root = kind => $(`#view-${kind}`);
  function skillDefaults() {return {enabled:true,domain:'',source:'',added_from:'',added_to:'',date_state:'all',sort:'added_desc'};}
  function skillQuery(s,offset=0) {
    const params=new URLSearchParams({q:s.q,offset:String(offset),limit:'40'});
    for(const [key,value] of Object.entries(s.filters))if(value!=='')params.set(key,String(value));
    return params;
  }
  function skillControlsHtml() {
    return `<div class="skill-status-groups" role="group" aria-label="启用状态">
      <button type="button" data-skill-enabled="true" aria-pressed="true"><span>已启用</span><b data-count-enabled>0</b></button>
      <button type="button" data-skill-enabled="false" aria-pressed="false"><span>未启用</span><b data-count-disabled>0</b></button></div>
      <div class="skill-filters" aria-label="技能筛选">
      <label>类型<select data-skill-filter="domain"><option value="">全部类型</option></select></label>
      <label>来源<select data-skill-filter="source"><option value="">全部来源</option><option value="builtin">内置参考</option><option value="archive">压缩包导入</option><option value="manual">手动／助手添加</option></select></label>
      <label>时间记录<select data-skill-filter="date_state"><option value="all">全部时间</option><option value="known">已记录时间</option><option value="unknown">时间未知</option></select></label>
      <label>导入／添加起始日<input type="date" data-skill-filter="added_from"></label>
      <label>导入／添加截止日<input type="date" data-skill-filter="added_to"></label>
      <label>排序<select data-skill-filter="sort"><option value="added_desc">最近导入／添加</option><option value="added_asc">最早导入／添加</option><option value="updated_desc">最近修改</option><option value="name_asc">名称 A–Z</option></select></label>
      </div><div class="skill-filter-footer"><span>日期按北京时间筛选；历史记录可能没有时间。</span><button type="button" class="btn sm" data-skill-reset>清除筛选</button></div>`;
  }
  function skillMeta(d) {
    const source=({builtin:'内置参考',archive:'压缩包导入',manual:'手动／助手添加'})[d.source_kind]||'自定义';
    const date=d.created_at==null?'时间未知':new Date(d.created_at*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
    return `${d.domain||'custom'} · ${source} · ${date}`;
  }
  function syncSkillControls(s,result) {
    const el=root('skill');s.counts=result.counts||s.counts;
    el.querySelector('[data-count-enabled]').textContent=s.counts.enabled;
    el.querySelector('[data-count-disabled]').textContent=s.counts.disabled;
    const select=el.querySelector('[data-skill-filter="domain"]'),domains=[...(result.facets?.domains||[])];
    if(s.filters.domain&&!domains.includes(s.filters.domain))domains.push(s.filters.domain);
    const signature=JSON.stringify(domains);
    if(select.dataset.domains!==signature){select.dataset.domains=signature;select.innerHTML='<option value="">全部类型</option>'+domains.map(domain=>`<option value="${esc(domain)}">${esc(domain)}</option>`).join('');}
    select.value=s.filters.domain;
    if(typeof selxSync==='function')el.querySelectorAll('[data-skill-filter]').forEach(field=>{if(field.tagName==='SELECT')selxSync(field);});
  }
  function mcpDefaults() {return {enabled:true,transport:'',source:'',risk:'',agent:'',added_from:'',added_to:'',date_state:'all',sort:'added_desc'};}
  function mcpControlsHtml() {
    return skillControlsHtml().replaceAll('data-skill-', 'data-mcp-')
      .replace('技能筛选','MCP 筛选')
      .replace('<label>类型<select data-mcp-filter="domain"><option value="">全部类型</option></select></label>',
        '<label>传输类型<select data-mcp-filter="transport"><option value="">全部类型</option><option value="stdio">STDIO · 本地命令</option><option value="http">HTTP · 流式连接</option><option value="sse">SSE · 事件流</option></select></label>')
      .replace('<option value="builtin">内置参考</option><option value="archive">压缩包导入</option><option value="manual">手动／助手添加</option>',
        '<option value="config">配置文件</option><option value="managed">管理页保存</option>')
      .replace('<label>时间记录', '<label>风险等级<select data-mcp-filter="risk"><option value="">全部风险</option><option value="readonly">只读</option><option value="intrusive">侵入性</option><option value="destructive">破坏性</option></select></label><label>适用 Agent<select data-mcp-filter="agent"><option value="">全部 Agent</option><option value="default">沿用角色默认范围</option></select></label><label>时间记录')
      .replaceAll('导入／添加','添加');
  }
  function mcpConnection(data) {
    if(data.transport==='stdio')return data.command||'尚未配置命令';
    try {const url=new URL(data.url);url.username='';url.password='';url.search='';url.hash='';return url.toString();}
    catch {return '尚未配置地址';}
  }
  function mcpMeta(item) {
    const data=item.data,risk=({readonly:'只读',intrusive:'侵入性',destructive:'破坏性'})[data.default_risk]||data.default_risk;
    const date=skillMeta({source_kind:'manual',created_at:item.created_at}).split(' · ').pop();
    return `${item.origin==='config'?'配置文件':'管理页保存'} · ${risk} · ${date}`;
  }
  function syncMcpControls(s,result) {
    const el=root('mcp');s.counts=result.counts||s.counts;
    el.querySelector('[data-count-enabled]').textContent=s.counts.enabled;
    el.querySelector('[data-count-disabled]').textContent=s.counts.disabled;
    const select=el.querySelector('[data-mcp-filter="agent"]'),agents=[...(result.facets?.agents||[])];
    if(s.filters.agent&&s.filters.agent!=='default'&&!agents.includes(s.filters.agent))agents.push(s.filters.agent);
    select.innerHTML='<option value="">全部 Agent</option><option value="default">沿用角色默认范围</option>'+agents.map(role=>`<option value="${esc(role)}">${esc(role)}</option>`).join('');
    select.value=s.filters.agent;
    if(typeof selxSync==='function')el.querySelectorAll('[data-mcp-filter]').forEach(field=>{if(field.tagName==='SELECT')selxSync(field);});
  }
  function shell(kind) {
    const [title,description]=titles[kind],el=root(kind);if(el.dataset.ready)return;el.dataset.ready='yes';
    el.innerHTML=`<header class="resource-head"><div><h2>${title}</h2><p>${description}</p></div><div class="resource-head-actions">${kind==='skill'?'<button class="btn" data-import>导入压缩包</button><input type="file" data-archives hidden multiple accept=".zip,.tar,.tar.gz,.tgz,.tar.bz2,.tbz2,.tar.xz,.txz,.rar,.7z,.tar.zst,.tzst">':''}<button class="btn" data-refresh>刷新</button><button class="btn primary" data-new>手动添加</button></div></header>
      ${kind==='skill'?'<p class="resource-import-help" id="skill-import-help">支持 ZIP、TAR、RAR、7z 等；可多选，每包最多 20 MiB，须包含 SKILL.md。只导入文本资料。</p><p class="resource-import-status" role="status"></p>':''}
      <div class="resource-layout"><section class="resource-catalog" aria-label="${title}目录">${kind==='skill'?skillControlsHtml():mcpControlsHtml()}<label class="resource-search">${kind==='skill'?'搜索名称、描述或类型':'搜索名称、命令、地址或 Agent'}<input type="search" data-search placeholder="输入关键词…" maxlength="200"></label><div class="resource-list-status" role="status"></div><div class="resource-list"></div><button class="btn resource-more" hidden>加载更多</button></section>
      ${kind==='mcp'?'<details class="skill-assistant mcp-search-settings"><summary>搜索服务 · Exa 配置</summary><div data-exa-settings></div></details>':''}<details class="skill-assistant"><summary>资源助手 · 对话添加和管理 ${kind==='skill'?'Skill':'MCP'}</summary><section class="resource-chat" aria-label="${title}专属管理对话"><header><div><b>资源助手</b><small>变更需确认后执行</small></div></header><div class="resource-chat-log" aria-live="polite"></div><div class="resource-proposals"></div><form class="resource-chat-form"><label class="sr-only" for="resource-message-${kind}">向${title}助手描述需求</label><textarea id="resource-message-${kind}" placeholder="${kind==='mcp'?'例如：帮我配置一个只读浏览器 MCP，请先给出安装方案':'例如：添加一份关于接口权限边界的参考技能'}" rows="3" maxlength="16000"></textarea><div><small>请勿在聊天中粘贴密钥</small><button class="btn primary" type="submit">发送</button></div><p class="resource-chat-status" role="status"></p></form></section></details></div>`;
    if(kind==='mcp')ExaSettings.mount(el.querySelector('[data-exa-settings]'));
    el.querySelector('[data-refresh]').onclick=()=>{load(kind);loadChat(kind);if(kind==='mcp')ExaSettings.load();};
    el.querySelector('[data-new]').onclick=()=>edit(kind);
    if(kind==='skill'){
      const input=el.querySelector('[data-archives]'),button=el.querySelector('[data-import]');
      button.setAttribute('aria-describedby','skill-import-help');
      button.onclick=()=>input.click();input.onchange=()=>importArchives(input);
    }
    {
      const prefix=kind==='skill'?'skill':'mcp';
      const setGroup=enabled=>{const s=stateFor(kind);s.filters.enabled=enabled;
        el.querySelectorAll(`[data-${prefix}-enabled]`).forEach(b=>b.setAttribute('aria-pressed',String((b.getAttribute(`data-${prefix}-enabled`)==='true')===enabled)));};
      el.querySelectorAll(`[data-${prefix}-enabled]`).forEach(b=>b.onclick=()=>{setGroup(b.getAttribute(`data-${prefix}-enabled`)==='true');load(kind);});
      el.querySelectorAll(`[data-${prefix}-filter]`).forEach(field=>field.onchange=()=>{
        const s=stateFor(kind),key=field.getAttribute(`data-${prefix}-filter`);s.filters[key]=field.value;
        if(key==='date_state'&&field.value==='unknown'){
          s.filters.added_from='';s.filters.added_to='';
          el.querySelector(`[data-${prefix}-filter="added_from"]`).value='';el.querySelector(`[data-${prefix}-filter="added_to"]`).value='';
        }else if(key.startsWith('added_')&&field.value&&s.filters.date_state==='unknown'){
          s.filters.date_state='all';el.querySelector(`[data-${prefix}-filter="date_state"]`).value='all';
        }
        load(kind);
      });
      el.querySelector(`[data-${prefix}-reset]`).onclick=()=>{
        const s=stateFor(kind),enabled=s.filters.enabled;s.filters=kind==='skill'?skillDefaults():mcpDefaults();s.filters.enabled=enabled;s.q='';
        el.querySelector('[data-search]').value='';el.querySelectorAll(`[data-${prefix}-filter]`).forEach(field=>field.value=s.filters[field.getAttribute(`data-${prefix}-filter`)]);load(kind);
      };
    }
    let timer;el.querySelector('[data-search]').oninput=e=>{const s=stateFor(kind);s.q=e.target.value.trim();s.generation++;clearTimeout(timer);timer=setTimeout(()=>load(kind),250);};
    el.querySelector('.resource-more').onclick=()=>load(kind,true);
    el.querySelector('.resource-chat-form').onsubmit=e=>send(kind,e);
  }
  async function importArchives(input) {
    const files=Array.from(input.files||[]);if(!files.length)return;
    const el=root('skill'),button=el.querySelector('[data-import]'),status=el.querySelector('.resource-import-status');
    button.disabled=true;input.disabled=true;let imported=0;const messages=[];
    try{
      for(const [index,file] of files.entries()){
        status.textContent=`正在导入 ${index+1}/${files.length}：${file.name}`;
        try{
          if(file.size>20*1024*1024)throw new Error('压缩包不得超过 20 MiB');
          const result=await api('/api/resources/skills/import',{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file});
          imported+=result.items.length;
          messages.push(`${file.name}：已导入 ${result.items.map(item=>item.skill_id).join('、')}`,...(result.warnings||[]));
        }catch(e){messages.push(`${file.name}：导入失败，${e.message}`);}
      }
      if(imported)await load('skill');
      status.textContent=messages.join('\n');
    }finally{button.disabled=false;input.disabled=false;input.value='';}
  }
  async function load(kind,append=false) {
    const s=stateFor(kind),version=++s.generation,status=root(kind).querySelector('.resource-list-status');
    status.textContent='正在读取…';
    if(!append){s.items=[];s.more=false;root(kind).querySelector('.resource-list').innerHTML='<div class="resource-empty">正在读取目录…</div>';root(kind).querySelector('.resource-more').hidden=true;}
    try{
      if(s.filters.added_from&&s.filters.added_to&&s.filters.added_from>s.filters.added_to)throw new Error('起始日期不能晚于截止日期');
      const offset=append?s.items.length:0;
      const path=`/api/resources/${kind==='skill'?'skills':'mcp'}`;
      const result=await api(path+'?'+skillQuery(s,offset));
      if(version!==s.generation)return;
      const rows=result.items||[];
      s.items=append?[...s.items,...rows]:rows;s.more=result.has_more;s.total=result.total??rows.length;
      if(kind==='skill')syncSkillControls(s,result);else syncMcpControls(s,result);
      status.textContent=`${s.filters.enabled?'已启用':'未启用'} · ${s.total} 条匹配，已显示 ${s.items.length} 条${kind==='mcp'?' · 变更新启动会话生效':''}`;render(kind);
    }catch(e){if(version===s.generation)status.textContent=`读取失败：${e.message}。调整筛选或点击刷新重试。`;}
  }
  function render(kind) {
    const s=stateFor(kind),list=root(kind).querySelector('.resource-list');
    list.innerHTML=s.items.map((item,i)=>{
      const d=kind==='mcp'?item.data:item;
      const title=kind==='mcp'?d.name:d.skill_id;
      const badge=kind==='skill'?(d.domain||'custom'):d.transport.toUpperCase();
      const subtitle=kind==='mcp'?mcpConnection(d):d.description||'尚无描述';
      const label=kind==='skill'?'技能':'MCP';
      return `<article class="resource-item"><button class="resource-select" data-select="${i}"><span class="resource-badge">${esc(badge)}</span><strong>${esc(title)}</strong><p>${esc(subtitle)}</p><small>${esc(kind==='mcp'?mcpMeta(item):skillMeta(d))}</small>${kind==='mcp'?`<small class="mcp-agent-scope">${esc(d.allowed_agents?.length?d.allowed_agents.join('、'):'沿用角色默认范围')}</small>`:''}</button><div class="skill-card-actions"><button class="skill-switch resource-toggle" type="button" role="switch" aria-checked="${!!d.enabled}" aria-label="启用${label} ${esc(title)}" data-toggle="${i}"><span class="skill-switch-track" aria-hidden="true"><span></span></span><span>${d.enabled?'已启用':'未启用'}</span></button><button type="button" class="skill-delete" data-delete="${i}" aria-label="删除${label} ${esc(title)}">删除</button></div></article>`;
    }).join('')||`<div class="resource-empty">当前没有${s.filters.enabled?'已启用':'未启用'}的匹配${kind==='skill'?'技能':'MCP'}<p>可切换分组、调整条件或清除筛选。</p></div>`;
    list.querySelectorAll('[data-select]').forEach(b=>b.onclick=()=>kind==='skill'?inspectSkill(s.items[+b.dataset.select]):edit(kind,s.items[+b.dataset.select]));
    list.querySelectorAll('[data-toggle]').forEach(b=>b.onclick=async()=>{const item=s.items[+b.dataset.toggle],d=kind==='mcp'?item.data:item;b.disabled=true;try{await api(`/api/resources/${kind==='mcp'?'mcp':'skills'}/${encodeURIComponent(kind==='mcp'?item.id:d.skill_id)}`,{method:'PUT',body:JSON.stringify({data:{enabled:!d.enabled},expected_version:item.version??d.version})});load(kind);}catch(e){toast(e.message,true);b.disabled=false;}});
    list.querySelectorAll('[data-delete]').forEach(button=>button.onclick=async()=>{
      const item=s.items[+button.dataset.delete],name=kind==='skill'?item.skill_id:item.id;
      if(!confirm(kind==='skill'?`删除 Skill「${name}」？删除后将从目录和后续参考检索中移除。`:`删除 MCP「${name}」？新启动的会话将不再连接，运行中的会话不受影响。不会卸载本地程序。`))return;
      button.disabled=true;
      try{await api(`/api/resources/${kind==='skill'?'skills':'mcp'}/${encodeURIComponent(name)}?expected_version=${item.version}`,{method:'DELETE'});toast(`${kind==='skill'?'Skill':'MCP'} 已删除`);await load(kind);}
      catch(e){toast(e.message,true);button.disabled=false;}
    });
    root(kind).querySelector('.resource-more').hidden=!s.more;
  }
  async function inspectSkill(item) {
    const generation=++inspecting;
    try{
      const data=await api('/api/resources/skills/'+encodeURIComponent(item.skill_id));
      if(generation===inspecting&&S.view==='skill')SkillBrowser.open(data,item=>edit('skill',item),()=>load('skill'));
    }catch(e){if(generation===inspecting)toast(e.message,true);}
  }
  async function edit(kind,item) {
    let data=null;
    if(kind==='mcp'&&item){try{item=await api('/api/resources/mcp/'+encodeURIComponent(item.id)+'/config');data=item.data;}catch(e){toast(e.message,true);return;}}
    if(kind==='skill'&&item){try{data=await api('/api/resources/skills/'+encodeURIComponent(item.skill_id));}catch(e){toast(e.message,true);return;}}
    editor={kind,version:data?.version??item?.version??0};$('#resource-editor-title').textContent=kind==='mcp'?'MCP 接入配置':'Skill 参考知识';$('#resource-key').value=kind==='mcp'?item?.id||'':item?.skill_id||'';$('#resource-key').disabled=!!item;
    const clean=Object.fromEntries(Object.entries(data||{}).filter(([key])=>!['id','version','origin','env_keys','headers_keys','name','skill_id','created_at','updated_at','source_kind'].includes(key)));
    if(kind==='skill'&&data?.files)delete clean.body;
    $('#resource-json').value=JSON.stringify(data?clean:kind==='mcp'?{transport:'stdio',command:'',args:[],enabled:true,default_risk:'intrusive',risk_by_action:false,allowed_agents:[]}:{description:'',domain:'custom',body:'适用条件：\n参考事实：\n证据与注意事项：',enabled:true},null,2);
    $('#resource-env').value='';$('#resource-headers').value='';$('#resource-secrets').hidden=kind!=='mcp';$('#resource-save-error').textContent='';$('#resource-editor').showModal();
  }
  async function save() {if(!editor)return;const button=$('#resource-save');button.disabled=true;try{const data=JSON.parse($('#resource-json').value),key=$('#resource-key').value.trim();if(!key)throw new Error('请填写唯一标识');if(editor.kind==='mcp'){if($('#resource-env').value.trim())data.env=JSON.parse($('#resource-env').value);if($('#resource-headers').value.trim())data.headers=JSON.parse($('#resource-headers').value);}await api(`/api/resources/${editor.kind==='mcp'?'mcp':'skills'}/${encodeURIComponent(key)}`,{method:'PUT',body:JSON.stringify({data,expected_version:editor.version})});$('#resource-editor').close();toast('配置已保存');load(editor.kind);}catch(e){$('#resource-save-error').textContent=e.message;}finally{button.disabled=false;}}
  function renderChat(kind,data) {
    const el=root(kind),log=el.querySelector('.resource-chat-log'),signature=JSON.stringify(data);if(el.dataset.chatSignature===signature)return;el.dataset.chatSignature=signature;
    const follow=log.scrollHeight-log.scrollTop-log.clientHeight<40;
    const usage=data.usage_total;if(usage)el.querySelector('.resource-chat header small').textContent=`独立对话 · 累计 ${Number(usage.used_tokens||0).toLocaleString('zh-CN')} token · ${usage.calls||0} 次调用`;
    log.innerHTML=data.messages.map(m=>`<div class="resource-message ${m.role==='user'?'user':'assistant'}"><small>${m.role==='user'?'你':'资源助手'} · ${esc(new Date(m.created_at*1000).toLocaleString('zh-CN',{hour12:false}))}</small><p>${esc(m.content)}</p></div>`).join('')||'<div class="resource-chat-welcome"><h3>描述配置需求</h3><p>描述要添加的工具或技能。执行前会列出待确认的变更。</p></div>';
    if(follow)log.scrollTop=log.scrollHeight;
    const proposals=el.querySelector('.resource-proposals');proposals.innerHTML=(data.proposals||[]).map(p=>`<details class="resource-proposal" ${['pending','applying'].includes(p.state)?'open':''}><summary>${esc(({pending:'待确认变更',applying:'正在安装与配置',completed:'变更已完成',failed:'执行失败',interrupted:'执行已中断'})[p.state]||p.state)}</summary><pre>${esc(JSON.stringify(p.proposal,null,2))}</pre>${p.result?`<p>${esc(p.result)}</p>`:''}${['pending','failed','interrupted'].includes(p.state)?`<button class="btn primary" data-apply="${esc(p.id)}">应用变更</button>`:''}</details>`).join('');
    proposals.querySelectorAll('[data-apply]').forEach(b=>b.onclick=async()=>{if(!b.dataset.confirmed){b.dataset.confirmed='yes';b.textContent='确认安装与配置';return;}b.disabled=true;try{await api(`/api/resources/proposals/${b.dataset.apply}/apply`,{method:'POST',body:JSON.stringify({confirm:true})});loadChat(kind);}catch(e){toast(e.message,true);b.disabled=false;}});
    if((data.proposals||[]).some(p=>p.state==='completed'))load(kind);
  }
  async function loadChat(kind) {try{const data=await api(`/api/resources/${kind}/chat`);if(S.view===kind)renderChat(kind,data);}catch(e){root(kind).querySelector('.resource-chat-status').textContent=`管理对话读取失败：${e.message}`;}}
  async function send(kind,event) {event.preventDefault();const form=event.currentTarget,input=form.querySelector('textarea'),text=input.value.trim();if(!text)return;const button=form.querySelector('button');button.disabled=true;const status=form.querySelector('.resource-chat-status');status.textContent='正在处理…';try{const result=await api(`/api/resources/${kind}/chat`,{method:'POST',body:JSON.stringify({message:text})});if(input.value.trim()===text)input.value='';if(S.view===kind)renderChat(kind,result);status.textContent=result.usage?`本次 ${result.usage.input_tokens+result.usage.output_tokens} token · ${result.usage.model}`:'';}catch(e){status.textContent=`处理失败：${e.message}。输入已保留，可重试。`;loadChat(kind);}finally{button.disabled=false;}}
  function open(kind) {clearInterval(poll);shell(kind);load(kind);loadChat(kind);if(kind==='mcp')ExaSettings.load();poll=setInterval(()=>{if(S.view!==kind){clearInterval(poll);return;}loadChat(kind);},2500);}
  document.querySelectorAll('[data-resource]').forEach(button=>button.onclick=()=>{S.cur=null;show(button.dataset.resource);renderSessions();});
  $('#resource-save').onclick=save;
  $('#resource-editor').addEventListener('close',()=>{$('#resource-env').value='';$('#resource-headers').value='';});
  return {open};
})();
