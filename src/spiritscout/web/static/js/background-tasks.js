"use strict";
const BackgroundTasks = (() => {
  let generation=0,timer=null,session=null,root=null,selected=null,busy=false;
  const states={running:I18n.t('运行中'),completed:I18n.t('已完成'),failed:I18n.t('失败'),stopped:I18n.t('已停止'),interrupted:I18n.t('已中断')};
  const active=token=>token===generation&&S.view==='session'&&S.tab==='background'&&S.cur===session;
  function close(){++generation;clearTimeout(timer);timer=null;session=null;selected=null;busy=false;}
  async function refresh(){
    if(busy)return;busy=true;const token=generation,id=session;
    try{
      const data=await api(`/api/sessions/${encodeURIComponent(id)}/background-tasks`);
      if(!active(token))return;
      if(!data.items.some(item=>item.job_id===selected))selected=data.items[0]?.job_id||null;
      root.querySelector('[data-background-list]').innerHTML=data.items.length?I18n.html`<table><thead><tr><th>任务</th><th>归属</th><th>状态</th><th>输出条件</th><th>操作</th></tr></thead><tbody>${data.items.map(item=>`<tr class="${item.job_id===selected?'traffic-record-selected':''}"><td><button class="traffic-link" data-background-select="${esc(item.job_id)}">${esc(item.label)}</button><small class="muted">${esc(item.job_id.slice(0,12))}</small></td><td>${esc(item.owner||I18n.t('未记录'))}<small class="muted">${esc(item.scope||I18n.t('会话'))}</small></td><td>${esc(states[item.status]||item.status)}${item.returncode===null?'':I18n.t` · 退出码 ${item.returncode}`}</td><td>${(item.watches||[]).map(w=>`${esc(w.contains)} · ${w.notified?(w.matched?I18n.t('已命中'):I18n.t('结束未命中')):I18n.t('监测中')}`).join('<br>')||I18n.t('完成时通知')}</td><td>${item.status==='running'?I18n.html`<button class="btn sm" data-background-stop="${esc(item.job_id)}">停止</button>`:''}</td></tr>`).join('')}</tbody></table>`:I18n.html('<p class="muted">还没有后台任务。Agent 启动后台 Shell 后会在这里展示，多项任务可同时运行。</p>');
      const item=data.items.find(row=>row.job_id===selected),detail=root.querySelector('[data-background-detail]');detail.hidden=!item;
      if(item){
        const html=I18n.html`<h3>${esc(item.label)} · ${esc(states[item.status]||item.status)}</h3><p class="muted">当前查看 ${esc(item.job_id)} · 每个输出流预览最近 2,000 字符</p>${item.error?`<p class="traffic-error">${esc(item.error)}</p>`:''}<div class="packet-panes"><section><h4>stdout</h4><pre>${esc(item.stdout||I18n.t('（暂无输出）'))}</pre></section><section><h4>stderr</h4><pre>${esc(item.stderr||I18n.t('（暂无输出）'))}</pre></section></div>`;
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
      root.querySelector('[data-background-message]').textContent=I18n.t('等待条件只影响依赖它的分支；其他 Agent 与后台任务继续执行。');
    }catch(error){if(active(token))root.querySelector('[data-background-message]').textContent=I18n.t`后台任务读取失败：${error.message}`;}
    finally{if(active(token))busy=false;}
  }
  function open(){
    close();if(S.view!=='session'||S.tab!=='background'||!S.cur)return;
    session=S.cur;root=$('#tab-background');
    root.innerHTML=I18n.html('<header class="traffic-head"><div><h2>后台任务</h2><p class="muted">查看并行 Shell 的输出、完成状态和条件通知</p></div><button class="btn sm" data-background-refresh>刷新</button></header><p data-background-message role="status"></p><div class="traffic-table-wrap" data-background-list></div><section class="traffic-detail" data-background-detail hidden></section>');
    root.querySelector('[data-background-refresh]').onclick=refresh;refresh();
    const token=generation;const tick=async()=>{if(!active(token))return;await refresh();if(active(token))timer=setTimeout(tick,3000);};timer=setTimeout(tick,3000);
  }
  return {open,close};
})();
