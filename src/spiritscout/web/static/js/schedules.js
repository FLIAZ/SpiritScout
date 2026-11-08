"use strict";
/* 独立自动任务：保存任务模板，每次触发新建执行会话。 */
const SchedulePage = (() => {
  let generation=0,timer=null,root=null,loading=false,again=false;
  const current=token=>S.view==='schedules'&&token===generation;
  const clock=value=>value?new Date(value*1000).toLocaleString(I18n.dateLocale,{timeZone:'Asia/Shanghai',hour12:false}):I18n.t('尚未触发');
  const names={ready:I18n.t('等待触发'),waiting:I18n.t('上一轮尚未结束'),dispatching:I18n.t('正在启动'),completed:I18n.t('已触发'),error:I18n.t('需要处理')};
  const runNames={created:I18n.t('准备中'),running:I18n.t('运行中'),completed:I18n.t('已完成'),failed:I18n.t('失败'),stopped:I18n.t('已停止'),interrupted:I18n.t('已中断'),error:I18n.t('启动失败'),pending:I18n.t('等待启动'),dispatched:I18n.t('已启动')};
  function notice(text,error=false){const box=root.querySelector('[data-schedule-notice]');box.textContent=text;box.classList.toggle('traffic-error',error);}
  function setOptions(name,items){
    const select=root.querySelector(`[name="${name}"]`),previous=select.value;
    const html=items.map(item=>`<option value="${esc(item.key)}">${esc(item.label)}</option>`).join('');
    if(select.innerHTML!==html)select.innerHTML=html;
    if(items.some(item=>item.key===previous))select.value=previous;
    if(typeof selxSync==='function')selxSync(select);
  }
  async function refresh(){
    if(loading){again=true;return;}loading=true;const token=generation;
    try{
      const [data,projects,models]=await Promise.all([api('/api/schedules'),api('/api/projects'),api('/api/models')]);
      if(!current(token))return;
      setOptions('project_id',[{key:'',label:I18n.t('未分类')},...(projects.projects||[]).map(item=>({key:item.project_id,label:item.name}))]);
      setOptions('model',ComposerModels.options(models));
      const list=root.querySelector('[data-schedule-list]');
      const html=data.items.length?data.items.map(item=>I18n.html`<article class="schedule-card" data-schedule-id="${esc(item.id)}"><header><h3>${esc(item.name)}</h3><span>${esc(!item.enabled&&item.state!=='completed'?I18n.t('已停用'):names[item.state]||item.state)}</span></header>
        <p class="muted">${item.kind==='standalone'?I18n.t('独立自动任务 · 每次新建会话'):I18n.t('旧版会话计划 · 可停用后新建独立任务')} · ${esc(({src:'SRC',ctf:'CTF',competition:I18n.t('智能体')})[item.task?.mode]||'')}</p>
        <p class="muted">${item.interval_seconds?I18n.t`每 ${item.interval_seconds/60} 分钟`:I18n.t('单次任务')} · ${item.enabled?I18n.t('下次触发'):I18n.t('计划时间')}：${clock(item.next_run)}（北京）</p><pre>${esc(item.message)}</pre>
        ${item.error?`<p class="traffic-error">${esc(item.error)}</p>`:''}
        <details ${item.runs?.length?'open':''}><summary>运行记录（最近 ${item.runs?.length||0} 次）</summary>${(item.runs||[]).map(run=>`<p class="schedule-run">${clock(run.created_at)} · ${esc(runNames[run.status]||run.status)} ${run.session_id?I18n.html`<button class="traffic-link" data-schedule-session="${esc(run.session_id)}">查看本次结果</button>`:''}${run.error?`<span class="traffic-error">${esc(run.error)}</span>`:''}</p>`).join('')||I18n.html('<p class="muted">到达计划时间后，每次执行会在这里单独留存。</p>')}</details>
        <footer><button class="btn sm" data-schedule-toggle="${esc(item.id)}" data-enabled="${!item.enabled}">${item.enabled?I18n.t('停用'):I18n.t('启用')}</button><button class="btn sm" data-schedule-delete="${esc(item.id)}">删除计划</button></footer></article>`).join(''):I18n.html('<p class="muted">还没有自动任务。直接描述要自动完成的工作，无需先创建会话。</p>');
      if(list._content!==html){
        const expanded=new Map([...list.querySelectorAll('[data-schedule-id]')].map(card=>[card.dataset.scheduleId,card.querySelector('details').open]));
        const focused=document.activeElement,card=focused?.closest('[data-schedule-id]');
        const focus=card&&list.contains(card)?{id:card.dataset.scheduleId,tag:focused.tagName,session:focused.dataset.scheduleSession,toggle:focused.dataset.scheduleToggle,delete:focused.dataset.scheduleDelete}:null;
        list.innerHTML=html;list._content=html;
        list.querySelectorAll('[data-schedule-id]').forEach(card=>{
          if(expanded.has(card.dataset.scheduleId))card.querySelector('details').open=expanded.get(card.dataset.scheduleId);
          if(focus?.id===card.dataset.scheduleId)card.querySelectorAll('summary,button').forEach(el=>{
            if(el.tagName===focus.tag&&el.dataset.scheduleSession===focus.session&&el.dataset.scheduleToggle===focus.toggle&&el.dataset.scheduleDelete===focus.delete)el.focus({preventScroll:true});
          });
        });
      }
      list.querySelectorAll('[data-schedule-session]').forEach(button=>button.onclick=()=>openSession(button.dataset.scheduleSession));
      list.querySelectorAll('[data-schedule-toggle]').forEach(button=>button.onclick=()=>mutate(`/api/schedules/${button.dataset.scheduleToggle}`,{method:'PATCH',body:JSON.stringify({enabled:button.dataset.enabled==='true'})},button));
      list.querySelectorAll('[data-schedule-delete]').forEach(button=>button.onclick=()=>mutate(`/api/schedules/${button.dataset.scheduleDelete}`,{method:'DELETE'},button));
      notice(I18n.t('自动任务独立执行并保存每次结果；同一计划上一轮尚未结束时等待，其他会话照常工作。'));
    }catch(error){if(current(token))notice(error.status===404?I18n.t('当前后端尚未加载自动任务接口，请在任务结束后重启后端。'):I18n.t`读取自动任务失败：${error.message}`,true);}
    finally{if(current(token)){loading=false;if(again){again=false;refresh();}}}
  }
  async function mutate(path,options,button){const token=generation;button.disabled=true;try{await api(path,options);if(current(token))await refresh();}catch(error){if(current(token))notice(I18n.t`操作失败：${error.message}`,true);}finally{if(current(token))button.disabled=false;}}
  function close(){++generation;clearTimeout(timer);timer=null;loading=false;again=false;}
  function open(){
    close();root=$('#view-schedules');
    root.innerHTML=I18n.html`<section class="schedule-page"><header class="traffic-head"><div><h2>自动任务</h2><p class="muted">描述工作并设置时间，每次自动创建新的执行会话，在这里查看结果。</p></div><button class="btn sm" data-schedule-refresh>刷新</button></header><p data-schedule-notice role="status"></p>
      <div class="schedule-layout"><form class="schedule-form"><h3>新建自动任务</h3><label>任务名称<input name="name" required maxlength="100" placeholder="例如：每日项目风险摘要"></label>
      <label>任务内容<textarea name="message" required maxlength="4000" rows="5" placeholder="描述希望自动完成的工作、输入资料和结果要求"></textarea></label>
      <label>执行模式<select name="mode"><option value="src">SRC</option><option value="ctf">CTF</option><option value="competition">智能体</option></select></label>
      <label>所属项目（可选）<select name="project_id"></select></label><label>模型<select name="model"></select></label>
      <label>目标（可选）<input name="target" maxlength="2000" placeholder="按任务需要填写目标地址"></label><label>范围说明（可选，每行一项）<textarea name="scope" rows="2"></textarea></label>
      <label>首次执行时间（本地时间）<input name="run_at" type="datetime-local" required></label><label>重复频率<select name="interval"><option value="">只执行一次</option><option value="3600">每小时</option><option value="86400">每天（间隔 24 小时）</option><option value="604800">每周</option></select></label><button class="btn" type="submit">创建自动任务</button></form>
      <section data-schedule-list aria-label="自动任务与运行记录"></section></div></section>`;
    const date=new Date(Date.now()+300000);date.setMinutes(date.getMinutes()-date.getTimezoneOffset());root.querySelector('[name="run_at"]').value=date.toISOString().slice(0,16);
    root.querySelector('[data-schedule-refresh]').onclick=refresh;
    root.querySelector('form').onsubmit=async event=>{
      event.preventDefault();const token=generation,form=event.currentTarget,button=form.querySelector('[type="submit"]');button.disabled=true;
      try{const read=name=>form.querySelector(`[name="${name}"]`).value;
        await api('/api/schedules',{method:'POST',body:JSON.stringify({name:read('name').trim(),message:read('message').trim(),mode:read('mode'),model:read('model'),project_id:read('project_id'),target:read('target').trim(),scope:read('scope').split('\n').map(x=>x.trim()).filter(Boolean),run_at:new Date(read('run_at')).toISOString(),interval_seconds:read('interval')?Number(read('interval')):null})});
        if(current(token)){form.querySelector('[name="name"]').value='';form.querySelector('[name="message"]').value='';await refresh();}
      }catch(error){if(current(token))notice(I18n.t`创建失败：${error.message}`,true);}finally{if(current(token))button.disabled=false;}
    };
    refresh();const token=generation;const tick=async()=>{if(!current(token))return;await refresh();if(current(token))timer=setTimeout(tick,10000);};timer=setTimeout(tick,10000);
  }
  return {open,close};
})();
