/* 会话档案：按需分页读取持久化数据；请求世代隔离会话切换和检索竞争。 */
const SessionArchive = (() => {
  const categories = [["all", I18n.t("全部档案")], ["fact", I18n.t("事实记录")], ["evidence", I18n.t("原始证据")],
    ["finding", I18n.t("待核验发现")], ["hypothesis", I18n.t("推测与缺口")], ["asset", I18n.t("资产记录")], ["file", I18n.t("文件")], ["outcome", I18n.t("任务结论")]];
  let session = null, kind = "all", query = "", items = [], selected = null;
  let counts = {}, total = 0, more = false, generation = 0, loading = false, lastLoad = 0, timer;
  const label = key => categories.find(c => c[0] === key)?.[1] || key;
  function filters() {
    const root = $("#archive-filters");
    const signature = JSON.stringify([kind, counts]);
    if (root.dataset.signature === signature) return;
    root.dataset.signature = signature;
    root.innerHTML = categories.map(([key, title]) => `<button type="button" data-kind="${key}" aria-pressed="${key === kind}" class="${key === kind ? "on" : ""}"><span>${title}</span><span class="archive-filter-count">${key === "all" ? Object.values(counts).reduce((a,b) => a+b,0) : counts[key] || 0}</span></button>`).join("");
  }
  function refsFor(item) {
    return (item.refs || []).map(ref => ({...ref,
      caption: [ref.method, ref.url || ref.title || ref.note || ref.kind].filter(Boolean).join(" ")}));
  }
  function detail() {
    const item = items.find(row => row.id === selected), root = $("#archive-detail");
    if (!item) {
      root.innerHTML = I18n.html('<div class="archive-placeholder">选择一条记录<br><small>在这里查看证据与来源</small></div>');
      delete root.dataset.record;
      return;
    }
    const record=JSON.stringify([session,item.layer,item.record_id]), previous=root.dataset.record===record ? root.querySelector('.archive-history') : null;
    const focus=previous?.contains(document.activeElement) ? document.activeElement : null;
    const positions=previous?[...previous.querySelectorAll('pre')].map(el=>[el,el.scrollTop,el.scrollLeft]):[];
    const top=root.scrollTop,left=root.scrollLeft;
    root.dataset.record=record;
    const refs = refsFor(item);
    root.innerHTML = I18n.html`<button class="btn sm archive-back" type="button" data-archive-back>← 返回档案列表</button>
      <div class="archive-detail-top"><span class="archive-kind" data-kind="${esc(item.kind)}">${esc(label(item.kind))}</span><span>${esc(item.status)}</span></div>
      <h4>${esc(item.title)}</h4>${item.text !== item.title ? `<p class="archive-detail-text">${esc(item.text)}</p>` : ""}
      ${item.target ? `<div class="archive-target">${esc(item.target)}</div>` : ""}
      <dl class="archive-meta"><dt>来源</dt><dd>${esc(item.source || I18n.t("未记录来源"))}</dd>
      <dt>保存位置</dt><dd>${esc(item.path || [item.layer, item.record_id].filter(Boolean).join(" / "))}</dd>
      ${item.version ? I18n.html`<dt>记录版本</dt><dd>v${esc(item.version)}</dd>` : ""}
      <dt>更新时间</dt><dd>${esc(formatTime(item.updated_at))}</dd>
      ${item.kind === "file" ? I18n.html`<dt>文件大小</dt><dd>${esc(fmtSize(item.size) || "0 B")}</dd>` : ""}</dl>
      ${refs.length ? I18n.html`<section class="archive-sources"><h5>引用来源 <span>${refs.length}</span></h5>${refs.map(ref => `<div class="archive-source"><code>${esc(ref.id || I18n.t("证据"))}</code><p>${esc(ref.caption)}</p>${ref.note ? `<small>${esc(ref.note)}</small>` : ""}${ref.record_id ? I18n.html`<small>黑板记录 ${esc(ref.record_id)}</small>` : ""}</div>`).join("")}</section>` : item.kind !== "file" ? I18n.html('<p class="archive-note">没有附带证据引用，请结合来源核验。</p>') : ""}
      ${item.record_id ? I18n.html`<details class="archive-history" data-layer="${esc(item.layer)}" data-record="${esc(item.record_id)}"><summary>版本历史</summary><p>历史版本仅供参考，使用前需复核。</p><div class="archive-history-list"></div></details>` : ""}
      ${item.kind === "file" ? I18n.html`<div class="archive-file-ops"><a class="btn archive-download" download href="/api/sessions/${encodeURIComponent(session)}/artifacts/download?path=${encodeURIComponent(item.path)}">下载文件</a><button type="button" class="btn" data-archive-share data-path="${esc(item.path)}">发送到会话</button></div>` : ""}`;
    const placeholder=root.querySelector('.archive-history');
    if(previous&&placeholder){
      placeholder.replaceWith(previous);
      positions.forEach(([el,top,left])=>{el.scrollTop=top;el.scrollLeft=left;});
      if(previous.dataset.version!==String(item.version||'')){
        previous.dataset.loaded='';
        if(previous.open)previous.dispatchEvent(new Event('toggle'));
      }
      focus?.focus({preventScroll:true});root.scrollTop=top;root.scrollLeft=left;
    }
    const history=root.querySelector('.archive-history');if(history)history.dataset.version=String(item.version||'');
  }
  function formatTime(value) {
    const date = new Date(typeof value === "number" ? value * 1000 : value);
    return Number.isNaN(date.getTime()) ? I18n.t("未记录") : date.toLocaleString(I18n.dateLocale, {hour12:false});
  }
  function render() {
    filters();
    $("#archive-count").textContent = I18n.t`${label(kind)} · ${total} 条`;
    const html = items.map(item => `<button type="button" class="archive-row ${selected === item.id ? "selected" : ""}" data-entry="${esc(item.id)}" aria-pressed="${selected === item.id}">
      <span class="archive-kind" data-kind="${esc(item.kind)}">${esc(label(item.kind))}</span><span class="archive-row-status">${esc(item.status)}</span>
      <strong>${esc(item.title)}</strong>${item.target || item.text !== item.title ? `<span class="archive-row-text">${esc(item.target || item.text)}</span>` : ""}
      <span class="archive-row-foot"><span>${esc(item.source || item.layer || I18n.t("会话产出"))}</span><span>${item.refs?.length ? I18n.t`${item.refs.length} 条证据` : esc(formatTime(item.updated_at))}</span></span></button>`).join("") ||
      `<div class="archive-empty"><h4>${query ? I18n.t("没有匹配的档案") : I18n.t("暂无会话记录")}</h4><p>${query ? I18n.t("试试其他关键词，或切换分类。") : I18n.t("本会话保存的发现、证据和文件会显示在这里。")}</p></div>`;
    const root = $("#archive-list");
    if (root.dataset.content !== html) { root.innerHTML = html; root.dataset.content = html; }
    $("#archive-more").hidden = !more;
    $("#archive-more").disabled = loading;
    // 详情仅在内容变化时更新，后台刷新不打断文本选择。
    const signature = JSON.stringify([session, items.find(row => row.id === selected)]);
    if ($("#archive-detail").dataset.signature !== signature) {
      $("#archive-detail").dataset.signature = signature;
      detail();
    }
  }
  async function load(append = false) {
    if (!session || S.cur !== session) return;
    const requestSession = session, current = ++generation;
    loading = true;
    $("#archive-sync").textContent = I18n.t("正在同步…");
    $("#archive-error").hidden = true;
    $("#archive-more").disabled = true;
    const offset = append ? items.length : 0;
    // 刷新已加载窗口，避免轮询把「加载更多」结果折回第一页。
    const windowSize = append ? 60 : Math.max(60, items.length);
    const limit = Math.min(200, windowSize);
    try {
      const page = await api(`/api/sessions/${encodeURIComponent(requestSession)}/deliverables?` +
        new URLSearchParams({kind, q:query, offset:String(offset), limit:String(limit)}));
      if (current !== generation || session !== requestSession || S.cur !== requestSession) return;
      while (!append && page.has_more && page.items.length < windowSize) {
        const next = await api(`/api/sessions/${encodeURIComponent(requestSession)}/deliverables?` +
          new URLSearchParams({kind, q:query, offset:String(page.items.length),
            limit:String(Math.min(200, windowSize - page.items.length))}));
        if (current !== generation || session !== requestSession || S.cur !== requestSession) return;
        page.items.push(...next.items); page.has_more = next.has_more && next.items.length > 0;
      }
      const merged = append ? [...items, ...page.items] : page.items;
      items = [...new Map(merged.map(row => [row.id, row])).values()];
      counts = page.counts; total = page.total; more = page.has_more;
      if (!items.some(row => row.id === selected)) selected = items[0]?.id || null;
      lastLoad = Date.now();
      $("#archive-sync").textContent = I18n.t("已同步 · ") + new Date().toLocaleTimeString(I18n.dateLocale, {hour12:false});
      render();
    } catch (error) {
      if (current !== generation || session !== requestSession) return;
      $("#archive-sync").textContent = I18n.t("同步失败");
      $("#archive-error").textContent = I18n.t`档案读取失败：${error.message}。可点击刷新重试。`;
      $("#archive-error").hidden = false;
    } finally {
      if (current === generation) { loading = false; $("#archive-more").disabled = false; }
    }
  }
  function reset(id) {
    clearTimeout(timer); generation++; session = id; kind = "all"; query = "";
    items = []; counts = {}; total = 0; more = false; selected = null; loading = false; lastLoad = 0;
    $("#archive-search").value = ""; $("#archive-sync").textContent = I18n.t("等待读取");
    $("#archive-error").hidden = true; render();
  }
  function changeFilter() {
    generation++; loading = false; items = []; selected = null; lastLoad = 0; render(); load();
  }
  async function exportResults() {
    const id = session, currentKind = kind, currentQuery = query, button = $("#archive-export");
    if (!id) return;
    button.disabled = true; button.textContent = I18n.t("正在整理…");
    try {
      const all = [];
      let offset = 0, hasMore = true;
      while (hasMore) {
        if (session !== id) throw new Error(I18n.t("已切换会话，请在当前会话重新导出"));
        const page = await api(`/api/sessions/${encodeURIComponent(id)}/deliverables?` +
          new URLSearchParams({kind:currentKind, q:currentQuery, offset:String(offset), limit:"200"}));
        all.push(...page.items); offset += page.items.length;
        hasMore = page.has_more && page.items.length > 0;
      }
      if (session !== id) throw new Error(I18n.t("已切换会话，请重新导出"));
      const unique = [...new Map(all.map(row => [row.id, row])).values()];
      const blob = new Blob([JSON.stringify({session_id:id, exported_at:new Date().toISOString(),
        filter:{kind:currentKind, query:currentQuery}, items:unique}, null, 2)], {type:"application/json"});
      const url = URL.createObjectURL(blob), link = document.createElement("a");
      link.href = url; link.download = I18n.t`${id}-档案.json`; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast(I18n.t`已导出 ${unique.length} 条档案`);
    } catch (error) { toast(error.message, true); }
    finally { button.disabled = false; button.textContent = I18n.t("导出当前结果"); }
  }
  function init() {
    $("#archive-refresh").onclick = () => load();
    $("#archive-more").onclick = () => load(true);
    $("#archive-export").onclick = exportResults;
    $("#archive-search").addEventListener("input", event => {
      query = event.target.value.trim(); generation++; clearTimeout(timer);
      timer = setTimeout(changeFilter, 250);
    });
    $("#archive-filters").onclick = event => {
      const button = event.target.closest("[data-kind]");
      if (!button) return;
      kind = button.dataset.kind; changeFilter();
      $("#archive-filters").querySelector(`[data-kind="${kind}"]`)?.focus();
    };
    $("#archive-list").onclick = event => {
      const button = event.target.closest("[data-entry]");
      if (!button) return;
      selected = button.dataset.entry; render();
      $("#archive-list").querySelectorAll("[data-entry]").forEach(row => {
        if (row.dataset.entry === selected) row.focus({preventScroll:true});
      });
      if (window.innerWidth <= 1100) $("#archive-detail").scrollIntoView({block:"nearest"});
    };
    $("#archive-detail").addEventListener("toggle", async event => {
      const box = event.target;
      if (!box.matches?.(".archive-history") || !box.open || box.dataset.loaded) return;
      box.dataset.loaded = "loading";
      const id = session;
      const list = box.querySelector(".archive-history-list");
      let before = 0;
      const fetchPage = async () => {
        try {
          const newest=Math.max(0,...[...list.querySelectorAll('[data-version]')].map(el=>Number(el.dataset.version)));
          const endpoint=`/api/sessions/${encodeURIComponent(id)}/record-history?`;
          const page = await api(endpoint +
            new URLSearchParams({layer:box.dataset.layer, record_id:box.dataset.record, before:String(before)}));
          if (session !== id || !box.isConnected) return;
          // 版本增长超过一页时补齐到已加载记录，保留读者展开的旧版本节点。
          let cursor=page.items.at(-1)?.version;
          while(!before&&newest&&page.has_more&&cursor>newest){
            const next=await api(endpoint+new URLSearchParams({layer:box.dataset.layer,record_id:box.dataset.record,before:String(cursor)}));
            if(session!==id||!box.isConnected)return;
            const last=next.items.at(-1)?.version;
            page.items.push(...next.items);page.has_more=next.has_more;
            if(!last||last>=cursor)break;
            cursor=last;
          }
          list.querySelector("button")?.remove();
          list.querySelector('[data-history-error]')?.remove();
          for (const item of page.items) {
            const existing=[...list.querySelectorAll('[data-version]')];
            if(existing.some(el=>el.dataset.version===String(item.version)))continue;
            const record = document.createElement("details"), title = document.createElement("summary"), pre = document.createElement("pre");
            record.dataset.version=String(item.version);
            title.textContent = I18n.t`历史 v${item.version} · ${formatTime(item.updated_at)}`;
            pre.textContent = JSON.stringify(item.data, null, 2); record.append(title,pre);
            list.insertBefore(record,existing.find(el=>Number(el.dataset.version)<item.version)||null);
          }
          const versions=[...list.querySelectorAll('[data-version]')].map(el=>Number(el.dataset.version));
          if(versions.length)before=Math.min(...versions);
          if (page.has_more) { const more = document.createElement("button"); more.className = "btn sm"; more.textContent = I18n.t("更早版本"); more.onclick = () => {more.disabled=true;fetchPage();}; list.append(more); }
          if (!list.children.length) list.textContent = I18n.t("尚未留存历史版本；升级前被覆盖的数据无法恢复。");
          box.dataset.loaded = "yes";
        } catch (error) {
          if(session!==id||!box.isConnected)return;
          box.dataset.loaded = "";
          const text=I18n.t`历史读取失败：${error.message}。重新展开可重试。`;
          if(list.querySelector('[data-version]')){
            let note=list.querySelector('[data-history-error]');
            if(!note){note=document.createElement('p');note.dataset.historyError='';note.setAttribute('role','status');list.append(note);}
            note.textContent=text;
          }else list.textContent=text;
        }
      };
      fetchPage();
    }, true);
    $("#archive-detail").onclick = async event => {
      if (event.target.closest("[data-archive-back]")) {
        $("#archive-search").scrollIntoView({block:"start"});
        $("#archive-search").focus({preventScroll:true});
      }
      const shareBtn = event.target.closest("[data-archive-share]");
      if (!shareBtn || shareBtn.disabled) return;
      const rel = shareBtn.dataset.path, id = session;
      shareBtn.disabled = true;
      try {
        await FileCards.share(id, rel);
        toast(I18n.t("已发送到会话"));
        // 跳回会话页并以增量流带回 file.shared 文件卡片（终态会话流会自行收尾）
        if (S.cur === id) {
          const tab = document.querySelector('[data-tab="chat"]');
          if (tab && S.tab !== "chat") tab.click();
          startStreams(false);
        }
      } catch (error) {
        toast(I18n.t`发送失败：${error.message}`, true);
      } finally {
        shareBtn.disabled = false;
      }
    };
    filters();
  }
  if (typeof document !== "undefined") init();
  return {reset, load, refsFor, refreshIfStale() {
    if (!loading && Date.now() - lastLoad > 12000) load();
  }};
})();
