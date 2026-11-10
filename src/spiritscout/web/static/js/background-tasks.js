"use strict";
const BackgroundTasks = (() => {
  let generation=0,timer=null,session=null,root=null,selected=null,busy=false;
  const states={running:I18n.t('运行中'),completed:I18n.t('已完成'),failed:I18n.t('失败'),stopped:I18n.t('已停止'),interrupted:I18n.t('已中断')};
  const terminalEmpty=I18n.t('（暂无输出）');
  const active=token=>token===generation&&S.view==='session'&&S.tab==='background'&&S.cur===session;
  const statusClass=status=>['running','completed','failed','stopped','interrupted'].includes(status)?status:'unknown';
  const outputSize=item=>(item.stdout||'').length+(item.stderr||'').length;
  const watchText=item=>(item.watches||[]).map(w=>`${esc(w.contains)} · ${w.notified?(w.matched?I18n.t('已命中'):I18n.t('结束未命中')):I18n.t('监测中')}`).join('<br>')||I18n.t('完成时通知');
  const timeText=value=>{
    if(!value)return I18n.t('时间未记录');
    const date=new Date(Number(value)*1000);
    return Number.isNaN(date.getTime())?I18n.t('时间未记录'):date.toLocaleString(I18n.dateLocale,{hour12:false});
  };
  function summary(items){
    const count=status=>items.filter(item=>item.status===status).length;
    const finished=items.filter(item=>item.status!=='running').length;
    return I18n.html`<div class="background-summary" aria-label="后台任务状态总览">
      <span class="background-summary-item is-running"><i aria-hidden="true"></i><strong>${count('running')}</strong><small>运行中</small></span>
      <span class="background-summary-item"><strong>${finished}</strong><small>已结束</small></span>
      <span class="background-summary-item is-failed"><strong>${count('failed')+count('interrupted')}</strong><small>异常</small></span>
      <span class="background-summary-item is-total"><strong>${items.length}</strong><small>任务总数</small></span>
    </div>`;
  }
  function queue(items){
    if(!items.length)return I18n.html`<div class="background-empty">
      <div class="background-empty-prompt" aria-hidden="true"><span>spirit@session</span><b>$</b><i></i></div>
      <h3>等待后台进程</h3>
      <p>Agent 启动耗时 Shell、监听器或扫描任务后，会在这里持续回传状态与输出。</p>
      <small>任务可以并行运行；等待条件只暂停依赖它的分支。</small>
    </div>`;
    return items.map((item,index)=>I18n.html`<article class="background-job background-job-${statusClass(item.status)} ${item.job_id===selected?'is-selected':''}">
      <span class="background-job-track" aria-hidden="true"><i></i></span>
      <button class="background-job-main" type="button" data-background-select="${esc(item.job_id)}" aria-current="${item.job_id===selected?'true':'false'}">
        <span class="background-job-index">${String(index+1).padStart(2,'0')}</span>
        <span class="background-job-copy">
          <span class="background-job-title">${esc(item.label||I18n.t('后台 Shell'))}</span>
          <span class="background-job-meta"><code>${esc(item.job_id.slice(0,12))}</code><span>${esc(item.owner||I18n.t('未记录'))}</span></span>
        </span>
        <span class="background-job-state">${esc(states[item.status]||item.status)}</span>
      </button>
      <div class="background-job-foot">
        <span>${esc(item.scope||I18n.t('会话'))}</span>
        <span>${outputSize(item).toLocaleString(I18n.dateLocale)} chars</span>
        ${item.status==='running'?I18n.html`<button class="background-stop" type="button" data-background-stop="${esc(item.job_id)}">停止</button>`:`<span>${item.returncode===null||item.returncode===undefined?I18n.t('已结束'):I18n.t`退出码 ${item.returncode}`}</span>`}
      </div>
    </article>`).join('');
  }
  function detailMarkup(item){
    const state=states[item.status]||item.status;
    return I18n.html`<header class="background-detail-head">
      <div><span class="background-kicker">SELECTED PROCESS</span><h3>${esc(item.label||I18n.t('后台 Shell'))}</h3></div>
      <span class="background-state background-state-${statusClass(item.status)}"><i aria-hidden="true"></i>${esc(state)}</span>
    </header>
    <dl class="background-facts">
      <div><dt>任务 ID</dt><dd><code>${esc(item.job_id)}</code></dd></div>
      <div><dt>启动时间</dt><dd>${esc(timeText(item.created_at))}</dd></div>
      <div><dt>归属 / 范围</dt><dd>${esc(item.owner||I18n.t('未记录'))} / ${esc(item.scope||I18n.t('会话'))}</dd></div>
      <div><dt>完成条件</dt><dd>${watchText(item)}</dd></div>
    </dl>
    ${item.error?`<p class="background-error" role="alert">${esc(item.error)}</p>`:''}
    <div class="background-terminals">
      <section class="background-terminal"><header><span><i aria-hidden="true"></i>stdout</span><small>${(item.stdout||'').length.toLocaleString(I18n.dateLocale)} chars · tail 2k</small></header><pre>${esc(item.stdout||terminalEmpty)}</pre></section>
      <section class="background-terminal background-terminal-error"><header><span><i aria-hidden="true"></i>stderr</span><small>${(item.stderr||'').length.toLocaleString(I18n.dateLocale)} chars · tail 2k</small></header><pre>${esc(item.stderr||terminalEmpty)}</pre></section>
    </div>`;
  }
  function close(){++generation;clearTimeout(timer);timer=null;session=null;selected=null;busy=false;}
  async function refresh(){
    if(busy)return;busy=true;const token=generation,id=session;
    try{
      const data=await api(`/api/sessions/${encodeURIComponent(id)}/background-tasks`);
      if(!active(token))return;
      if(!data.items.some(item=>item.job_id===selected))selected=data.items[0]?.job_id||null;
      root.querySelector('.background-workbench').classList.toggle('is-empty',!data.items.length);
      root.querySelector('[data-background-summary]').innerHTML=summary(data.items);
      root.querySelector('[data-background-list]').innerHTML=queue(data.items);
      const item=data.items.find(row=>row.job_id===selected),detail=root.querySelector('[data-background-detail]');detail.hidden=!item;
      if(item){
        const html=detailMarkup(item);
        if(detail._content!==html){
          const positions=detail.dataset.job===item.job_id?[...detail.querySelectorAll('pre')].map(el=>[el.scrollTop,el.scrollLeft]):[];
          detail.innerHTML=html;detail._content=html;detail.dataset.job=item.job_id;
          detail.querySelectorAll('pre').forEach((el,i)=>{if(positions[i]){el.scrollTop=positions[i][0];el.scrollLeft=positions[i][1];}});
        }
      }else{detail.replaceChildren();delete detail._content;delete detail.dataset.job;}
      root.querySelectorAll('[data-background-select]').forEach(button=>button.onclick=()=>{selected=button.dataset.backgroundSelect;refresh();});
      root.querySelectorAll('[data-background-stop]').forEach(button=>button.onclick=async()=>{
        button.disabled=true;try{await api(`/api/sessions/${encodeURIComponent(id)}/background-tasks/${encodeURIComponent(button.dataset.backgroundStop)}/stop`,{method:'POST'});if(active(token))refresh();}catch(error){if(active(token))root.querySelector('[data-background-message]').textContent=error.message;}
      });
      root.querySelector('[data-background-message]').textContent=data.items.length?I18n.t('自动刷新中 · 输出仅展示每个流最近 2,000 字符'):I18n.t('控制台待机中 · 暂无后台任务');
    }catch(error){if(active(token))root.querySelector('[data-background-message]').textContent=I18n.t`后台任务读取失败：${error.message}`;}
    finally{if(active(token))busy=false;}
  }
  function open(){
    close();if(S.view!=='session'||S.tab!=='background'||!S.cur)return;
    session=S.cur;root=$('#tab-background');
    root.innerHTML=I18n.html(`<section class="background-console">
      <header class="background-console-head">
        <div><span class="background-kicker">PROCESS CONTROL</span><h2>后台任务</h2><p>监看并行 Shell、输出条件与任务退出状态。</p></div>
        <div class="background-console-actions"><span data-background-message role="status">正在读取任务…</span><button class="btn sm" type="button" data-background-refresh>刷新</button></div>
      </header>
      <div data-background-summary></div>
      <div class="background-workbench">
        <aside class="background-queue" aria-label="后台任务队列"><header><span>任务队列</span><small>PROCESS QUEUE</small></header><div class="background-queue-list" data-background-list></div></aside>
        <section class="background-detail" data-background-detail hidden></section>
      </div>
    </section>`);
    root.querySelector('[data-background-refresh]').onclick=refresh;refresh();
    const token=generation;const tick=async()=>{if(!active(token))return;await refresh();if(active(token))timer=setTimeout(tick,3000);};timer=setTimeout(tick,3000);
  }
  return {open,close};
})();
