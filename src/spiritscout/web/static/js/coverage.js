/* 覆盖账本只读取显式记录；切换会话使旧请求失效，证据按需读取。 */
const CoveragePage = (() => {
  const labels={untested:'未见尝试',running:'进行中',blocked:'受阻',pending:'待验证',refuted:'本次未复现',confirmed:'已独立确认'};
  const sourceLabels={available:'可定位',missing:'未找到',changed:'版本已变化',restricted:'私密记录'};
  let session='',offset=0,version=0,controller=null,timer=null,items=[];
  const root=()=>document.querySelector('#tab-coverage');
  const active=()=>S.view==='session'&&S.tab==='coverage'&&S.cur;
  function stop(){++version;controller?.abort();controller=null;clearTimeout(timer);timer=null;}
  function mount(){
    root().innerHTML=I18n.html`<div class="coverage-head"><div><h3>覆盖与证据</h3><p class="muted">按资产、实际身份和测试方向核对。记录仅反映已有探索，不代表目标已全面测试。</p></div><button class="btn sm" data-refresh>刷新</button></div><div class="coverage-counts"></div><form class="coverage-filters"><input type="search" name="q" maxlength="200" placeholder="搜索资产、测试方向或原因" aria-label="搜索覆盖记录"><select name="status" aria-label="覆盖状态"><option value="">全部状态</option>${Object.entries(labels).map(([k,v])=>`<option value="${k}">${esc(I18n.t(v))}</option>`).join('')}</select><select name="identity" aria-label="实际身份"><option value="">全部身份</option></select><select name="test_type" aria-label="测试方向"><option value="">全部方向</option></select><button class="btn sm" type="submit">筛选</button></form><p data-status role="status"></p><div class="coverage-rows"></div><div class="coverage-pages"><button class="btn sm" data-prev>上一页</button><span></span><button class="btn sm" data-next>下一页</button></div>`;
    root().querySelector('[data-refresh]').onclick=()=>load();
    root().querySelector('form').onsubmit=e=>{e.preventDefault();offset=0;load();};
    root().querySelector('[data-prev]').onclick=()=>{offset=Math.max(0,offset-50);load();};
    root().querySelector('[data-next]').onclick=()=>{offset+=50;load();};
  }
  function options(name,values,label){const el=root().querySelector(`[name="${name}"]`),value=el.value;
    el.innerHTML=`<option value="">${esc(I18n.t(label))}</option>`+values.map(v=>`<option value="${esc(v)}">${esc(v)}</option>`).join('');el.value=value;if(typeof selxSync==='function')selxSync(el);}
  async function load(){
    if(!active())return;
    stop();const ticket=version,sid=S.cur;controller=new AbortController();
    const form=root().querySelector('form'),params=new URLSearchParams(new FormData(form));params.set('offset',String(offset));
    const status=root().querySelector('[data-status]');status.textContent=I18n.t('正在读取…');
    try{
      const data=await api(`/api/sessions/${encodeURIComponent(sid)}/coverage?${params}`,{signal:controller.signal});
      if(ticket!==version||sid!==S.cur||!active())return;
      if(offset&&offset>=data.total){offset=0;return load();}
      items=data.items;options('identity',data.identities,'全部身份');options('test_type',data.test_types,'全部方向');
      root().querySelector('.coverage-counts').innerHTML=Object.entries(labels).map(([k,v])=>`<span class="tag coverage-${k}">${esc(I18n.t(v))} <b>${Number(data.counts[k]||0)}</b></span>`).join('');
      status.textContent=data.truncated?I18n.t('记录量较大，当前展示有界快照；统计不代表完整覆盖。'):I18n.t('未标注的身份和方向保留为空，不推断为匿名或已测试。');
      root().querySelector('.coverage-rows').innerHTML=items.length?`<table class="coverage-table"><thead><tr>${['资产 / 接口','实际身份','测试方向','当前结论','依据与缺口'].map(t=>`<th scope="col">${esc(I18n.t(t))}</th>`).join('')}</tr></thead><tbody>${items.map((r,i)=>`<tr><td>${esc(r.asset)}</td><td>${esc(r.identity||I18n.t('未标注'))}</td><td>${esc(r.test_type||I18n.t('未标注'))}</td><td><span class="tag coverage-${r.status}">${esc(I18n.t(labels[r.status]||r.status))}</span></td><td><details><summary>${esc(r.text)} · ${r.evidence.length} ${I18n.t('条引用')}${r.evidence_missing?' · '+r.evidence_missing+' '+I18n.t('条待补证'):''}</summary><p>${esc(r.reason||I18n.t('未记录结论依据或阻塞原因'))}</p><button type="button" class="btn sm" data-record="${i}">${I18n.t('查看探索原文')}</button>${r.evidence.map((e,j)=>`<p><button type="button" class="btn sm" data-evidence="${i}:${j}">${esc(e.ref)}</button> <span class="muted">${esc(I18n.t(sourceLabels[e.state]||e.state))}</span></p>`).join('')||`<p class="muted">${I18n.t('暂无证据引用')}</p>`}<div data-evidence-detail></div></details></td></tr>`).join('')}</tbody></table>`:I18n.html('<p class="resource-empty">没有匹配的覆盖记录。探索记录尚未建立时，不推断测试已完成。</p>');
      root().querySelectorAll('[data-evidence],[data-record]').forEach(b=>b.onclick=()=>inspect(b,sid,ticket));
      root().querySelector('[data-prev]').disabled=offset===0;root().querySelector('[data-next]').disabled=!data.has_more;
      root().querySelector('.coverage-pages span').textContent=data.total?`${offset+1}–${offset+items.length} / ${data.total}`:'0';
    }catch(e){if(ticket===version&&e.name!=='AbortError')status.textContent=I18n.t('读取失败：')+e.message;}
    finally{if(ticket===version&&active())timer=setTimeout(()=>{if(!root().querySelector('details[open]'))load();else schedule();},30000);}
  }
  function schedule(){clearTimeout(timer);if(active())timer=setTimeout(()=>{if(!root().querySelector('details[open]'))load();else schedule();},30000);}
  async function inspect(button,sid,ticket){
    const parts=button.dataset.evidence?.split(':').map(Number);
    const ref=parts?items[parts[0]].evidence[parts[1]].ref:items[Number(button.dataset.record)].ref;
    const box=button.closest('details').querySelector('[data-evidence-detail]');button.disabled=true;
    try{const data=await api(`/api/sessions/${encodeURIComponent(sid)}/coverage/evidence?ref=${encodeURIComponent(ref)}`);
      if(ticket!==version||sid!==S.cur)return;
      box.innerHTML=data.file&&typeof FileCards!=='undefined'?FileCards.card({rel:data.file,name:data.file.split('/').pop()}):
        !data.resolved?`<p class="muted">${esc(I18n.t(({missing:'未找到对应记录，请补充实际执行、文件或黑板引用。',changed:'引用版本已变化，需要重新核对。',restricted:'凭据原文不在覆盖页展示。'})[data.state]||'当前引用无法读取'))}</p>`:
        `<p>${esc(I18n.t('引用可定位，仍需结合原始输出检查结论。'))}</p>${data.tool?`<p>${esc(data.tool)} · ${esc(data.execution_status||'')}</p>`:''}<details><summary>${I18n.t('原始证据记录')}</summary><pre class="fd-pre">${esc(JSON.stringify(data.data||data,null,2))}</pre></details>`;
    }catch(e){if(ticket===version)box.textContent=I18n.t('读取失败：')+e.message;}finally{button.disabled=false;}
  }
  function navigate(){if(!active()){stop();return;}if(session!==S.cur){session=S.cur;offset=0;mount();}load();}
  document.addEventListener('visibilitychange',()=>{if(document.hidden)stop();else if(active())navigate();});
  return {navigate};
})();
