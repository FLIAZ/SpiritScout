/* 会话档案：按需分页读取持久化数据；请求世代隔离会话切换和检索竞争。 */
const SessionArchive = (() => {
  const categories = [["all", "全部档案"], ["fact", "有据事实"], ["evidence", "原始证据"],
    ["finding", "待核验发现"], ["hypothesis", "推测与缺口"], ["asset", "资产记录"], ["file", "文件产出"], ["outcome", "交付结论"]];
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
      root.innerHTML = '<div class="archive-placeholder">选择一条记录<br><small>在这里查看证据与来源</small></div>';
      return;
    }
    const refs = refsFor(item);
    root.innerHTML = `<button class="btn sm archive-back" type="button" data-archive-back>← 返回档案列表</button>
      <div class="archive-detail-top"><span class="archive-kind" data-kind="${esc(item.kind)}">${esc(label(item.kind))}</span><span>${esc(item.status)}</span></div>
      <h4>${esc(item.title)}</h4>${item.text !== item.title ? `<p class="archive-detail-text">${esc(item.text)}</p>` : ""}
      ${item.target ? `<div class="archive-target">${esc(item.target)}</div>` : ""}
      <dl class="archive-meta"><dt>来源</dt><dd>${esc(item.source || "未记录来源")}</dd>
      <dt>保存位置</dt><dd>${esc(item.path || [item.layer, item.record_id].filter(Boolean).join(" / "))}</dd>
      ${item.version ? `<dt>记录版本</dt><dd>v${esc(item.version)}</dd>` : ""}
      <dt>更新时间</dt><dd>${esc(formatTime(item.updated_at))}</dd>
      ${item.kind === "file" ? `<dt>文件大小</dt><dd>${esc(fmtSize(item.size) || "0 B")}</dd>` : ""}</dl>
      ${refs.length ? `<section class="archive-sources"><h5>证据链 <span>${refs.length}</span></h5>${refs.map(ref => `<div class="archive-source"><code>${esc(ref.id || "证据")}</code><p>${esc(ref.caption)}</p>${ref.note ? `<small>${esc(ref.note)}</small>` : ""}${ref.record_id ? `<small>黑板记录 ${esc(ref.record_id)}</small>` : ""}</div>`).join("")}</section>` : item.kind !== "file" ? '<p class="archive-note">没有附带证据引用，请结合来源核验。</p>' : ""}
      ${item.kind === "file" ? `<a class="btn archive-download" download href="/api/sessions/${encodeURIComponent(session)}/artifacts/download?path=${encodeURIComponent(item.path)}">下载文件 ↗</a>` : ""}`;
  }
  function formatTime(value) {
    const date = new Date(typeof value === "number" ? value * 1000 : value);
    return Number.isNaN(date.getTime()) ? "未记录" : date.toLocaleString("zh-CN", {hour12:false});
  }
  function render() {
    filters();
    $("#archive-count").textContent = `${label(kind)} · ${total} 条`;
    const html = items.map(item => `<button type="button" class="archive-row ${selected === item.id ? "selected" : ""}" data-entry="${esc(item.id)}" aria-pressed="${selected === item.id}">
      <span class="archive-kind" data-kind="${esc(item.kind)}">${esc(label(item.kind))}</span><span class="archive-row-status">${esc(item.status)}</span>
      <strong>${esc(item.title)}</strong>${item.target || item.text !== item.title ? `<span class="archive-row-text">${esc(item.target || item.text)}</span>` : ""}
      <span class="archive-row-foot"><span>${esc(item.source || item.layer || "会话产出")}</span><span>${item.refs?.length ? `${item.refs.length} 条证据` : esc(formatTime(item.updated_at))}</span></span></button>`).join("") ||
      `<div class="archive-empty"><span class="archive-empty-mark" aria-hidden="true">◇</span><h4>${query ? "没有匹配的档案" : "这里将保存本次会话的积累"}</h4><p>${query ? "试试其他关键词，或切换分类。" : "已保存的证据、发现与文件会自动出现在这里。"}</p></div>`;
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
    $("#archive-sync").textContent = "正在同步…";
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
      $("#archive-sync").textContent = "已同步 · " + new Date().toLocaleTimeString("zh-CN", {hour12:false});
      render();
    } catch (error) {
      if (current !== generation || session !== requestSession) return;
      $("#archive-sync").textContent = "同步失败";
      $("#archive-error").textContent = `档案读取失败：${error.message}。可点击刷新重试。`;
      $("#archive-error").hidden = false;
    } finally {
      if (current === generation) { loading = false; $("#archive-more").disabled = false; }
    }
  }
  function reset(id) {
    clearTimeout(timer); generation++; session = id; kind = "all"; query = "";
    items = []; counts = {}; total = 0; more = false; selected = null; loading = false; lastLoad = 0;
    $("#archive-search").value = ""; $("#archive-sync").textContent = "等待读取";
    $("#archive-error").hidden = true; render();
  }
  function changeFilter() {
    generation++; loading = false; items = []; selected = null; lastLoad = 0; render(); load();
  }
  async function exportResults() {
    const id = session, currentKind = kind, currentQuery = query, button = $("#archive-export");
    if (!id) return;
    button.disabled = true; button.textContent = "正在整理…";
    try {
      const all = [];
      let offset = 0, hasMore = true;
      while (hasMore) {
        if (session !== id) throw new Error("已切换会话，请在当前会话重新导出");
        const page = await api(`/api/sessions/${encodeURIComponent(id)}/deliverables?` +
          new URLSearchParams({kind:currentKind, q:currentQuery, offset:String(offset), limit:"200"}));
        all.push(...page.items); offset += page.items.length;
        hasMore = page.has_more && page.items.length > 0;
      }
      if (session !== id) throw new Error("已切换会话，请重新导出");
      const unique = [...new Map(all.map(row => [row.id, row])).values()];
      const blob = new Blob([JSON.stringify({session_id:id, exported_at:new Date().toISOString(),
        filter:{kind:currentKind, query:currentQuery}, items:unique}, null, 2)], {type:"application/json"});
      const url = URL.createObjectURL(blob), link = document.createElement("a");
      link.href = url; link.download = `${id}-档案.json`; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast(`已导出 ${unique.length} 条档案`);
    } catch (error) { toast(error.message, true); }
    finally { button.disabled = false; button.textContent = "导出当前结果"; }
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
    $("#archive-detail").onclick = event => {
      if (event.target.closest("[data-archive-back]")) {
        $("#archive-search").scrollIntoView({block:"start"});
        $("#archive-search").focus({preventScroll:true});
      }
    };
    filters();
  }
  if (typeof document !== "undefined") init();
  return {reset, load, refsFor, refreshIfStale() {
    if (!loading && Date.now() - lastLoad > 12000) load();
  }};
})();
