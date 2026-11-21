"use strict";
const BackgroundTasks = (() => {
  let generation=0,timer=null,session=null,root=null,selected=null,busy=false,selectionSerial=0,archiveSupported=true,items=[],detailItem=null,panes={};
  const states={running:I18n.t('运行中'),completed:I18n.t('已完成'),failed:I18n.t('失败'),stopped:I18n.t('已停止'),interrupted:I18n.t('已中断')};
  const archiveStates={complete:I18n.t('持续存档 · 随会话保留'),legacy:I18n.t('旧版预览 · 原始输入和完整输出未存档'),limited:I18n.t('存档不完整 · 已达到输出配额，命令仍继续执行'),error:I18n.t('存档不完整 · 写入失败，请检查磁盘'),stopped:I18n.t('存档已保存 · 任务被停止，末尾输出可能未捕获'),interrupted:I18n.t('存档不完整 · 上次运行意外中断'),cleared:I18n.t('存档已由用户清理')};
  const active=token=>token===generation&&S.view==='session'&&S.tab==='background'&&S.cur===session;
  const statusClass=status=>['running','completed','failed','stopped','interrupted'].includes(status)?status:'unknown';
  const size=value=>FileCards.sizeText(Number(value)||0);
  const timeText=value=>value?new Date(Number(value)*1000).toLocaleString(I18n.dateLocale,{hour12:false}):I18n.t('未记录');
  const watchText=item=>(item.watches||[]).map(w=>`${esc(w.contains)} · ${w.notified?(w.matched?I18n.t('已命中'):I18n.t('结束未命中')):I18n.t('监测中')}`).join('<br>')||I18n.t('完成时通知');
  const base=()=>`/api/sessions/${encodeURIComponent(session)}/background-tasks`;
  const jobUrl=()=>`${base()}/${encodeURIComponent(selected)}`;
  function setHtml(node,html){if(node._content!==html){node.innerHTML=html;node._content=html;}}
  function summary(data){
    const count=status=>data.counts?.[status]||0;
    return I18n.html`<div class="background-summary" aria-label="后台任务状态总览">
      <span class="background-summary-item ${count('running')?'is-running':''}"><i aria-hidden="true"></i><strong>${count('running')}</strong><small>运行中</small></span>
      <span class="background-summary-item"><strong>${data.total-count('running')}</strong><small>已结束</small></span>
      <span class="background-summary-item is-failed"><strong>${count('failed')+count('interrupted')}</strong><small>异常</small></span>
      <span class="background-summary-item is-total"><strong>${data.total}</strong><small>历史任务</small></span>
    </div>`;
  }
  function queue(){
    if(!items.length)return I18n.html`<div class="background-empty"><div class="background-empty-prompt" aria-hidden="true"><span>spirit@session</span><b>$</b><i></i></div><h3>等待后台进程</h3><p>后台命令与输出会持续写入本地档案，结束后仍可回看。</p><small>档案随会话保留，不自动进入模型上下文。</small></div>`;
    return items.map((item,index)=>I18n.html`<article class="background-job background-job-${statusClass(item.status)} ${item.job_id===selected?'is-selected':''}">
      <span class="background-job-track" aria-hidden="true"><i></i></span>
      <button class="background-job-main" type="button" data-background-select="${esc(item.job_id)}" aria-current="${item.job_id===selected?'true':'false'}">
        <span class="background-job-index">${String(index+1).padStart(2,'0')}</span><span class="background-job-copy"><span class="background-job-title">${esc(item.label||I18n.t('后台 Shell'))}</span><span class="background-job-meta"><code>${esc(item.job_id.slice(0,12))}</code><span>${esc(item.owner||I18n.t('未记录'))}</span></span></span><span class="background-job-state">${esc(states[item.status]||item.status)}</span>
      </button><div class="background-job-foot"><span>${esc(item.scope||I18n.t('会话'))}</span><span>${size((item.stdout_bytes||0)+(item.stderr_bytes||0))}</span>${item.status==='running'?I18n.html`<button class="background-stop" type="button" data-background-stop="${esc(item.job_id)}">停止</button>`:`<span>${item.returncode==null?I18n.t('已结束'):I18n.t`退出码 ${item.returncode}`}</span>`}</div>
    </article>`).join('');
  }
  function paneMarkup(stream){return I18n.html`<section class="background-terminal ${stream==='stderr'?'background-terminal-error':''}" data-stream="${stream}">
    <header><span><i aria-hidden="true"></i>${stream}</span><small data-size></small><button class="background-terminal-action" type="button" data-download="${stream}">下载</button></header>
    <form class="background-output-search" data-search="${stream}"><input type="search" aria-label="${esc(I18n.t`搜索 ${stream} 完整存档`)}" placeholder="${esc(I18n.t('搜索完整存档'))}" maxlength="1024"><button type="submit">查找</button><button type="button" data-next="${stream}" disabled>下一处</button></form>
    <div class="background-output-nav"><button type="button" data-older="${stream}" disabled>更早输出</button><span data-output-status role="status">正在读取存档…</span><button type="button" data-newer="${stream}" disabled>较新输出</button><button type="button" data-latest="${stream}">回到最新</button></div>
    <pre tabindex="0" aria-label="${stream}"></pre></section>`;}
  function mountDetail(item){
    const detail=root.querySelector('[data-background-detail]');detail.hidden=false;
    detail.innerHTML=I18n.html`<header class="background-detail-head"><div><span class="background-kicker">COMMAND ARCHIVE</span><h3>${esc(item.label||I18n.t('后台 Shell'))}</h3></div><span class="background-state" data-state></span></header>
      <dl class="background-facts" data-facts></dl><p class="background-archive-state" data-archive-state role="status"></p><p class="background-error" data-execution-error hidden></p>
      <section class="background-command"><header><span>原始命令</span><div><button class="btn sm" type="button" data-copy-command>复制</button><button class="btn sm" type="button" data-download="command">下载命令</button><button class="btn sm" type="button" data-clear-archive>清理存档</button></div></header><pre tabindex="0" data-command></pre></section>
      <div class="background-terminals">${paneMarkup('stdout')}${paneMarkup('stderr')}</div>`;
    detail.querySelector('[data-command]').textContent=item.command??I18n.t('原始命令未存档');
    for(const stream of ['stdout','stderr']){
      const node=detail.querySelector(`[data-stream="${stream}"]`),pre=node.querySelector('pre');
      panes[stream]={node,request:0,start:0,end:0,total:-1,following:true,busy:false,query:'',match:null};
      pre.onscroll=()=>{const pane=panes[stream];if(pane.node===node&&pre.scrollHeight-pre.clientHeight-pre.scrollTop>24)pane.following=false;};
      node.querySelector('form').onsubmit=event=>{event.preventDefault();const query=node.querySelector('input').value.trim();loadOutput(stream,{query,after:0,latest:!query});};
    }
    updateDetail(item);
  }
  function updateDetail(item){
    const detail=root.querySelector('[data-background-detail]'),state=detail.querySelector('[data-state]');
    state.className=`background-state background-state-${statusClass(item.status)}`;
    state.innerHTML=`<i aria-hidden="true"></i>${esc(states[item.status]||item.status)}`;
    setHtml(detail.querySelector('[data-facts]'),I18n.html`<div><dt>启动 / 结束</dt><dd>${esc(timeText(item.created_at))} / ${esc(timeText(item.finished_at))}</dd></div><div><dt>工作目录</dt><dd><code>${esc(item.workdir||I18n.t('未记录'))}</code></dd></div><div><dt>归属 / 范围</dt><dd>${esc(item.owner||I18n.t('未记录'))} / ${esc(item.scope||I18n.t('会话'))}</dd></div><div><dt>完成条件 / 退出码</dt><dd>${watchText(item)} · ${esc(item.returncode==null?'—':item.returncode)}</dd></div>`);
    const error=detail.querySelector('[data-execution-error]');error.hidden=!item.error;error.textContent=item.error||'';
    const note=detail.querySelector('[data-archive-state]');note.textContent=item.archive_error||archiveStates[item.archive_state]||I18n.t('存档状态未知');
    note.classList.toggle('is-incomplete',!['complete','cleared'].includes(item.archive_state));
    if(item.archive_state==='cleared'){
      delete item.command;detail.querySelector('[data-command]').textContent=I18n.t('存档已清理');
      for(const pane of Object.values(panes)){pane.request++;pane.following=false;pane.total=0;pane.node.querySelector('pre').textContent=I18n.t('存档已清理');}
    }
    detail.querySelector('[data-copy-command]').disabled=!item.command;
    detail.querySelector('[data-clear-archive]').disabled=!archiveSupported||item.status==='running'||item.archive_state==='cleared';
    detail.querySelectorAll('[data-search] input,[data-search] button').forEach(node=>{node.disabled=!archiveSupported||['legacy','cleared'].includes(item.archive_state);});
    detail.querySelectorAll('[data-download]').forEach(button=>{button.disabled=['legacy','cleared'].includes(item.archive_state)||(button.dataset.download==='command'&&!item.command);});
  }
  async function selectItem(item){
    selected=item.job_id;detailItem=null;panes={};const token=generation,serial=++selectionSerial,id=selected,url=jobUrl(),detail=root.querySelector('[data-background-detail]');
    detail.hidden=false;detail.textContent=I18n.t('正在读取命令档案…');renderQueue();
    try{
      const data=archiveSupported?await api(url):{...item,archive_state:"legacy"};if(!active(token)||selected!==id||serial!==selectionSerial)return;
      detailItem=data;mountDetail(data);await Promise.all(['stdout','stderr'].map(stream=>loadOutput(stream,{latest:true})));
    }catch(error){if(active(token)&&selected===id&&serial===selectionSerial){detail.textContent=I18n.t`命令档案读取失败：${error.message}`;}}
  }
  function renderQueue(){const node=root.querySelector('[data-background-list]'),position=node.scrollTop;setHtml(node,queue());node.scrollTop=position;}
  async function loadOutput(stream,options={}){
    const pane=panes[stream];if(!pane)return;const token=generation,id=selected,request=++pane.request;
    pane.busy=true;const params=new URLSearchParams({stream});
    if(options.before!==undefined)params.set('before',options.before);
    const query=options.query??'';if(query){params.set('q',query);params.set('after',options.after??0);}
    const status=pane.node.querySelector('[data-output-status]');status.textContent=I18n.t('正在读取…');
    try{
      const data=archiveSupported?await api(`${jobUrl()}/output?${params}`):{text:detailItem?.[stream]||"",start:0,end:0,total_bytes:0,has_earlier:false,match:null,archive_state:"legacy"};
      if(!active(token)||selected!==id||panes[stream]!==pane||request!==pane.request)return;
      pane.preview=data.text;pane.start=data.start;pane.end=data.end;pane.total=data.total_bytes;pane.query=query;pane.match=data.match;pane.following=!!options.latest;
      const pre=pane.node.querySelector('pre'),position=pre.scrollTop;
      const text=data.text||(data.archive_state==='cleared'?I18n.t('存档已清理'):data.archive_state==='legacy'?I18n.t('旧版未保存该输出流的原文'):['error','limited','interrupted','stopped'].includes(data.archive_state)?I18n.t('没有可读取的存档，不能确认该输出流是否产生过内容'):detailItem?.status==='running'?I18n.t('该输出流尚未产生内容'):I18n.t('该输出流没有输出'));
      if(pre.textContent!==text||pane.renderQuery!==query){pre.innerHTML=query&&data.match!=null?esc(text).split(esc(query)).join(`<mark>${esc(query)}</mark>`):esc(text);pane.renderQuery=query;if(pane.following)pre.scrollTop=pre.scrollHeight;else pre.scrollTop=options.before!==undefined?pre.scrollHeight:position;}
      const mark=pre.querySelector('mark');if(query&&data.match!=null&&mark)pre.scrollTop=Math.max(0,mark.offsetTop-pre.offsetTop-24);
      pane.node.querySelector('[data-size]').textContent=data.archive_state==='legacy'?I18n.t('旧版预览'):size(data.total_bytes);
      status.textContent=query?(data.match==null?I18n.t('未找到匹配内容'):I18n.t`已定位 · ${size(data.match)}`):data.archive_state==='legacy'?I18n.t('旧版末尾预览'):data.has_earlier?I18n.t`已加载 ${size(data.start)}–${size(data.end)}`:I18n.t('已加载到开头');
      pane.node.querySelector('[data-older]').disabled=!data.has_earlier;
      pane.node.querySelector('[data-newer]').disabled=data.end>=data.total_bytes;
      pane.node.querySelector('[data-next]').disabled=!query||data.match==null;
    }catch(error){if(active(token)&&selected===id&&panes[stream]===pane&&request===pane.request)status.textContent=I18n.t`存档读取失败：${error.message}`;}
    finally{if(panes[stream]===pane&&request===pane.request)pane.busy=false;}
  }
  function close(){++generation;clearTimeout(timer);timer=null;session=null;selected=null;busy=false;items=[];detailItem=null;panes={};}
  async function refresh(older=false){
    if(busy)return;busy=true;const token=generation,url=base(),offset=older?items.length:0;
    try{
      const data=await api(`${url}?offset=${offset}`);if(!active(token))return;
      archiveSupported=typeof data.total==='number';
      if(!archiveSupported){data.total=data.items.length;data.counts={};for(const item of data.items){item.archive_state='legacy';data.counts[item.status]=(data.counts[item.status]||0)+1;}}
      const merged=new Map((older?items:data.items).map(item=>[item.job_id,item]));
      for(const item of (older?data.items:items))if(!merged.has(item.job_id))merged.set(item.job_id,item);
      items=[...merged.values()].sort((a,b)=>b.created_at-a.created_at);
      setHtml(root.querySelector('[data-background-summary]'),summary(data));renderQueue();
      root.querySelector('.background-workbench').classList.toggle('is-empty',!items.length);
      const more=root.querySelector('[data-more-tasks]');more.hidden=items.length>=data.total;
      root.querySelector('[data-storage]').textContent=archiveSupported?I18n.t`磁盘占用 ${size(data.disk_bytes)} · 输出配额 ${size(data.stored_bytes)} / ${size(data.limit_bytes)}`:I18n.t('重启服务后启用完整存档 · 当前展示旧版预览');
      if(!detailItem&&items.length)await selectItem(items.find(row=>row.job_id===selected)||items[0]);
      else if(detailItem){
        const item=items.find(row=>row.job_id===selected);if(item){detailItem={...detailItem,...item};updateDetail(detailItem);}
        for(const stream of ['stdout','stderr']){
          const pane=panes[stream];if(!pane||pane.busy)continue;
          const pre=pane.node.querySelector('pre'),selection=window.getSelection?.();
          if(selection&&!selection.isCollapsed&&pre.contains(selection.anchorNode))pane.following=false;
          if(pane.following&&(archiveSupported?pane.total!==(detailItem[`${stream}_bytes`]||0):pane.preview!==(detailItem[stream]||'')))await loadOutput(stream,{latest:true});
        }
      }
      root.querySelector('[data-background-message]').textContent=items.length?I18n.t(archiveSupported?'自动刷新中 · 完整输出分段读取':'自动刷新中 · 旧版末尾预览'):I18n.t('暂无后台任务');
    }catch(error){if(active(token))root.querySelector('[data-background-message]').textContent=I18n.t`后台任务读取失败：${error.message}`;}
    finally{if(active(token))busy=false;}
  }
  function open(){
    close();if(S.view!=='session'||S.tab!=='background'||!S.cur)return;session=S.cur;root=$('#tab-background');
    root.innerHTML=I18n.html`<section class="background-console"><header class="background-console-head"><div><span class="background-kicker">PROCESS CONTROL</span><h2>后台任务</h2><p>回看命令、完整输出与退出状态，档案保留在本地。</p></div><div class="background-console-actions"><span data-background-message role="status">正在读取任务…</span><button class="btn sm" type="button" data-background-refresh>刷新</button></div></header><div data-background-summary></div><div class="background-storage-bar"><span data-storage></span><span>完整日志按需读取，不自动加入模型上下文</span></div><div class="background-workbench"><aside class="background-queue" aria-label="后台任务队列"><header><span>任务队列</span><small>COMMAND HISTORY</small></header><div class="background-queue-list" data-background-list></div><button class="background-more" type="button" data-more-tasks hidden>加载更早任务</button></aside><section class="background-detail" data-background-detail hidden></section></div></section>`;
    root.onclick=async event=>{
      const button=event.target.closest('button');if(!button||button.disabled)return;const token=generation;
      if(button.hasAttribute('data-background-refresh'))return refresh();
      if(button.hasAttribute('data-more-tasks'))return refresh(true);
      if(button.dataset.backgroundSelect){const item=items.find(row=>row.job_id===button.dataset.backgroundSelect);if(item&&selected!==item.job_id)selectItem(item);return;}
      if(button.dataset.download){
        const suffix=button.dataset.download==='command'?'':detailItem.archive_state!=='complete'?'-partial':detailItem.status==='running'?'-snapshot':'';
        return FileCards.saveFile(`${jobUrl()}/download?stream=${button.dataset.download}`,`background-${selected}-${button.dataset.download}${suffix}.txt`);
      }
      if(button.dataset.older)return loadOutput(button.dataset.older,{before:panes[button.dataset.older].start});
      if(button.dataset.newer){const pane=panes[button.dataset.newer];return loadOutput(button.dataset.newer,{before:Math.min(pane.total,pane.end+32768)});}
      if(button.dataset.latest){const pane=panes[button.dataset.latest];pane.node.querySelector('input').value='';return loadOutput(button.dataset.latest,{latest:true});}
      if(button.dataset.next){const pane=panes[button.dataset.next];return loadOutput(button.dataset.next,{query:pane.query,after:pane.match+1});}
      const id=selected;
      try{
        if(button.hasAttribute('data-copy-command')){await navigator.clipboard.writeText(detailItem.command);if(active(token)&&selected===id)toast(I18n.t('命令已复制'));return;}
        if(button.dataset.backgroundStop){button.disabled=true;await api(`${base()}/${encodeURIComponent(button.dataset.backgroundStop)}/stop`,{method:'POST'});}
        if(button.hasAttribute('data-clear-archive')){
          if(!confirm(I18n.t('清理此任务的后台命令和输出存档？任务状态仍保留，清理后无法恢复；不会删除沙箱文件或其他审计记录。')))return;
          button.disabled=true;await api(`${jobUrl()}/archive`,{method:'DELETE'});
          if(active(token)&&selected===id){items=items.filter(row=>row.job_id!==id);await selectItem({job_id:id});}
        }
        if(active(token))refresh();
      }catch(error){if(active(token)){root.querySelector('[data-background-message]').textContent=error.message;button.disabled=false;}}
    };
    refresh();const token=generation;const tick=async()=>{if(!active(token))return;await refresh();if(active(token))timer=setTimeout(tick,3000);};timer=setTimeout(tick,3000);
  }
  return {open,close};
})();
