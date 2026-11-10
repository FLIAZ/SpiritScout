/* 测试记录归入资产工作区；只读显式关联，请求按会话与视图隔离。 */
const CoveragePage = (() => {
  const labels={untested:'尚未开始',running:'进行中',blocked:'受阻',pending:'待验证',refuted:'本次未复现',confirmed:'已独立确认'};
  const sourceLabels={available:'可定位',missing:'未找到',changed:'版本已变化',restricted:'私密记录'};
  let session='',view='assets',assetRef='',assetTitle='',offset=0,version=0,controller=null,timer=null;
  let summaryController=null,summaryVersion=0;
  const expandedSummaries=new Map();
  const root=()=>document.querySelector('#asset-tests');
  const active=()=>S.view==='session'&&S.tab==='assets'&&view==='tests';
  function stop(){++version;controller?.abort();controller=null;clearTimeout(timer);timer=null;++summaryVersion;summaryController?.abort();summaryController=null;}
  function scopeMarkup(){return assetRef ? `<span>${esc(assetTitle || assetRef)}</span><button type="button" class="btn sm" data-clear-asset>${I18n.t('查看全部测试')}</button>` : `<span>${I18n.t('全部资产的显式测试记录')}</span>`;}
  function mount(){
    root().innerHTML=I18n.html`<div class="coverage-head"><div><h3>测试记录</h3><p class="muted">按资产、实际身份和测试方向查看明确记录的验证；发现资产或访问成功不代表已完成安全测试。</p></div><button class="btn sm" data-refresh>刷新</button></div><div class="coverage-scope">${scopeMarkup()}</div><div class="coverage-counts"></div><form class="coverage-filters"><input type="search" name="q" maxlength="200" placeholder="搜索资产、测试方向或原因" aria-label="搜索测试记录"><select name="status" aria-label="筛选测试状态"><option value="">全部状态</option>${Object.entries(labels).map(([k,v])=>`<option value="${k}">${esc(I18n.t(v))}</option>`).join('')}</select><select name="identity" aria-label="实际身份"><option value="">全部身份</option></select><select name="test_type" aria-label="测试方向"><option value="">全部方向</option></select><button class="btn sm" type="submit">筛选</button></form><p data-status role="status"></p><div class="coverage-rows"></div><div class="coverage-pages"><button class="btn sm" data-prev>上一页</button><span></span><button class="btn sm" data-next>下一页</button></div>`;
    root().querySelector('[data-refresh]').onclick=()=>load();
    root().querySelector('form').onsubmit=e=>{e.preventDefault();offset=0;load();};
    root().querySelector('[data-prev]').onclick=()=>{offset=Math.max(0,offset-50);load();};
    root().querySelector('[data-next]').onclick=()=>{offset+=50;load();};
    root().querySelector('[data-clear-asset]')?.addEventListener('click',()=>openForAsset('',''));
  }
  function syncView(){
    const library=document.querySelector('#asset-library');if(library)library.hidden=view!=='assets';
    if(root())root().hidden=view!=='tests';
    document.querySelectorAll('[data-asset-view]').forEach(button=>{
      button.setAttribute('aria-pressed',String(button.dataset.assetView===view));
      button.onclick=()=>{stop();view=button.dataset.assetView;syncView();if(view==='tests')load();};
    });
  }
  function ensureSession(){if(session!==S.cur){stop();session=S.cur;view='assets';assetRef='';assetTitle='';offset=0;expandedSummaries.clear();if(root())root().replaceChildren();}}
  function options(name,values,label){const el=root().querySelector(`[name="${name}"]`),value=el.value;
    el.innerHTML=`<option value="">${esc(I18n.t(label))}</option>`+values.map(v=>`<option value="${esc(v)}">${esc(v)}</option>`).join('');el.value=value;if(typeof selxSync==='function')selxSync(el);}
  function wireEvidence(container){container.querySelectorAll('[data-archive-ref]').forEach(button=>button.onclick=()=>SessionArchive.openReference(button.dataset.archiveRef));}
  function recordBody(row){return `<p>${esc(row.reason || I18n.t('未记录结论依据或阻塞原因'))}</p><button type="button" class="btn sm" data-archive-ref="${esc(row.ref)}">${I18n.t('查看测试原文')}</button>${row.evidence.map(e=>`<p><button type="button" class="btn sm" data-archive-ref="${esc(e.ref)}">${esc(e.ref)}</button> <span class="muted">${esc(I18n.t(sourceLabels[e.state]||e.state))}</span></p>`).join('') || `<p class="muted">${I18n.t('暂无证据引用')}</p>`}`;}
  async function load(){
    if(!active()||!root())return;
    if(!root().querySelector('form'))mount();
    stop();const ticket=version,sid=S.cur;controller=new AbortController();
    const params=new URLSearchParams(new FormData(root().querySelector('form')));
    params.set('offset',String(offset));params.set('include_unattempted','false');if(assetRef)params.set('asset_ref',assetRef);
    const status=root().querySelector('[data-status]');status.textContent=I18n.t('正在读取…');
    try{
      const data=await api(`/api/sessions/${encodeURIComponent(sid)}/coverage?${params}`,{signal:controller.signal});
      if(ticket!==version||sid!==S.cur||!active())return;
      if(offset&&offset>=data.total){offset=0;return load();}
      options('identity',data.identities,'全部身份');options('test_type',data.test_types,'全部方向');
      root().querySelector('.coverage-counts').innerHTML=Object.entries(labels).filter(([k])=>data.counts[k]).map(([k,v])=>`<span class="tag coverage-${k}">${esc(I18n.t(v))} <b>${Number(data.counts[k])}</b></span>`).join('');
      status.textContent=data.truncated?I18n.t('记录量较大，当前展示有界快照；统计不代表完整覆盖。'):I18n.t('未标注的身份和方向保留为空，不推断为匿名或已测试。');
      root().querySelector('.coverage-rows').innerHTML=data.items.length?`<table class="coverage-table"><thead><tr>${['资产 / 接口','实际身份','测试方向','当前结论','依据与缺口'].map(t=>`<th scope="col">${esc(I18n.t(t))}</th>`).join('')}</tr></thead><tbody>${data.items.map(r=>`<tr><td>${esc(r.asset)}</td><td>${esc(r.identity||I18n.t('未标注'))}</td><td>${esc(r.test_type||I18n.t('未标注'))}</td><td><span class="tag coverage-${r.status}">${esc(I18n.t(labels[r.status]||r.status))}</span></td><td><details data-coverage-ref="${esc(r.ref)}"><summary>${esc(r.text)} · ${r.evidence.length} ${I18n.t('条引用')}${r.evidence_missing?' · '+r.evidence_missing+' '+I18n.t('条待补证'):''}</summary>${recordBody(r)}</details></td></tr>`).join('')}</tbody></table>`:I18n.html('<p class="resource-empty">尚无匹配的测试记录。资产目录仍保留已发现的接口；没有关联记录不等于没有访问过，也不代表目标安全。</p>');
      wireEvidence(root());
      root().querySelector('[data-prev]').disabled=offset===0;root().querySelector('[data-next]').disabled=!data.has_more;
      root().querySelector('.coverage-pages span').textContent=data.total?`${offset+1}–${offset+data.items.length} / ${data.total}`:'0';
    }catch(e){if(ticket===version&&sid===S.cur&&active()&&e.name!=='AbortError')status.textContent=I18n.t('读取失败：')+e.message;}
    finally{if(ticket===version&&active())schedule();}
  }
  function schedule(){clearTimeout(timer);if(active())timer=setTimeout(()=>{if(!root().querySelector('details[open]'))load();else schedule();},30000);}
  function navigate(){
    ensureSession();if(S.view!=='session'||S.tab!=='assets'){stop();return;}
    syncView();if(view==='tests')load();
  }
  function openForAsset(ref,title){
    if(!S.cur)return;ensureSession();stop();assetRef=String(ref||'').replace(/@\d+$/,'');assetTitle=title||'';offset=0;view='tests';
    if(root())root().replaceChildren();
    if(S.tab!=='assets')document.querySelector('[data-tab="assets"]')?.click();else navigate();
  }
  async function assetSummary(record,panel){
    const box=panel.querySelector('[data-asset-tests-summary]');if(!box)return;
    summaryController?.abort();summaryController=new AbortController();const ticket=++summaryVersion,sid=S.cur,ref=`${record.layer}:${record.record_id}`;
    const current=()=>ticket===summaryVersion&&sid===S.cur&&S.tab==='assets'&&panel._curId===record.id&&box.isConnected&&panel.querySelector('[data-asset-tests-summary]')===box;
    try{
      const data=await api(`/api/sessions/${encodeURIComponent(sid)}/coverage?`+new URLSearchParams({asset_ref:ref,include_unattempted:'false',limit:'3'}),{signal:summaryController.signal});
      if(!current())return;
      const expansionKey=sid+'|'+ref,expanded=expandedSummaries.get(expansionKey)||new Set();
      box.innerHTML=`<h4>${I18n.t('关联测试记录')} · ${Number(data.total)}</h4>${data.items.map(row=>`<details data-test-ref="${esc(row.ref.replace(/@\d+$/,''))}" ${expanded.has(row.ref.replace(/@\d+$/,'')) ? 'open' : ''}><summary><span class="tag coverage-${row.status}">${esc(I18n.t(labels[row.status]||row.status))}</span> ${esc(row.test_type||row.text)} · ${esc(row.identity||I18n.t('身份未标注'))}</summary>${recordBody(row)}</details>`).join('') || `<p class="muted">${I18n.t('尚无关联测试记录；发现或访问该资产不代表已经验证安全。')}</p>`}<button type="button" class="btn sm" data-view-tests>${I18n.t('查看全部测试记录')}</button>`;
      box.querySelectorAll('[data-test-ref]').forEach(detail=>detail.ontoggle=()=>{if(detail.open)expanded.add(detail.dataset.testRef);else expanded.delete(detail.dataset.testRef);expandedSummaries.set(expansionKey,expanded);});
      wireEvidence(box);box.querySelector('[data-view-tests]').onclick=()=>openForAsset(ref,record.title);
    }catch(error){if(current()&&error.name!=='AbortError'){box.innerHTML=`<h4>${I18n.t('关联测试记录')}</h4><p role="status">${esc(I18n.t('读取失败：')+error.message)}</p><button type="button" class="btn sm" data-retry-tests>${I18n.t('重试')}</button>`;box.querySelector('[data-retry-tests]').onclick=()=>assetSummary(record,panel);}}
  }
  document.addEventListener('visibilitychange',()=>{if(document.hidden)stop();else navigate();});
  return {navigate,openForAsset,assetSummary};
})();
