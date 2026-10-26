/* 经验列表、来源会话和提炼记录。 */
const ExperiencePage = (() => {
  const state = {items: [], offset: 0, q: "", tag: "", generation: 0, more: false, total: 0};
  const root = () => $("#view-exp");
  const time = t => t ? new Date(t * 1000).toLocaleString("zh-CN", {hour12: false}) : "未记录";

  function shell() {
    const el = root();
    if (el.dataset.ready) return;
    el.dataset.ready = "yes";
    el.innerHTML = `
      <header class="resource-head">
        <div>
          <h2>经验提炼</h2>
          <p>查看会话提炼的经验、适用场景和来源；不合适的条目可以删除。</p>
        </div>
        <div class="resource-head-actions"><button class="btn" data-refresh>刷新</button></div>
      </header>
      <div class="exp-tiles" data-tiles></div>
      <div class="exp-toolbar">
        <label class="resource-search">搜索经验<input type="search" data-search placeholder="内容、场景或标签…" maxlength="100"></label>
        <div class="exp-tags" data-tags role="group" aria-label="按标签过滤"></div>
      </div>
      <div class="exp-layout">
        <section aria-label="经验列表">
          <div class="resource-list-status" data-status role="status"></div>
          <div class="exp-list" data-list></div>
          <button class="btn resource-more" data-more hidden>加载更多</button>
        </section>
        <aside class="exp-history" aria-label="提炼过程记录">
          <h3>提炼记录</h3>
          <p class="exp-history-note">查看各次提炼的结果和失败原因。</p>
          <div data-hist></div>
        </aside>
      </div>`;
    el.querySelector("[data-refresh]").onclick = () => load(true);
    let timer;
    el.querySelector("[data-search]").oninput = e => {
      state.q = e.target.value.trim();
      clearTimeout(timer);
      timer = setTimeout(() => load(), 250);
    };
    el.querySelector("[data-more]").onclick = () => load(false, true);
  }

  async function load(resetTags = false, append = false) {
    shell();
    if (resetTags) state.tag = "";
    const version = ++state.generation;
    const status = root().querySelector("[data-status]");
    if (!append) status.textContent = "正在读取…";
    try {
      const params = {limit: "40", offset: append ? String(state.offset) : "0"};
      if (state.q) params.q = state.q;
      if (state.tag) params.tag = state.tag;
      const data = await api("/api/experience?" + new URLSearchParams(params));
      if (version !== state.generation) return;
      state.items = append ? [...state.items, ...(data.items || [])] : (data.items || []);
      state.offset = state.items.length;
      state.more = !!data.has_more;
      state.total = data.total || 0;
      state.stats = data;
      status.textContent = state.total
        ? `${state.total} 条经验`
        : (state.q || state.tag ? "没有匹配的经验，请更换关键词或标签。" : "");
      render(data);
    } catch (e) {
      status.textContent = `读取失败：${e.message}。点击刷新重试。`;
    }
  }

  function render(data) {
    const el = root();
    renderTiles(data);
    renderTags(data);
    renderList(data);
    renderHistory(data.history || []);
  }

  function renderTiles(data) {
    const last = data.last_ts ? time(data.last_ts).slice(5, 16) : "—";
    root().querySelector("[data-tiles]").innerHTML = `
      <div class="exp-tile"><span>经验</span><b>${Number(data.total || 0).toLocaleString("zh-CN")}</b><small>筛选结果</small></div>
      <div class="exp-tile"><span>来源会话</span><b>${Number(data.sources || 0).toLocaleString("zh-CN")}</b></div>
      <div class="exp-tile"><span>标签</span><b>${(data.tags || []).length}</b></div>
      <div class="exp-tile"><span>最近提炼</span><b class="exp-tile-time">${esc(last)}</b></div>`;
  }

  function renderTags(data) {
    const box = root().querySelector("[data-tags]");
    const tags = data.tags || [];
    const chips = [`<button class="resource-chip ${!state.tag ? "on" : ""}" data-tag="">全部</button>`];
    for (const t of tags.slice(0, 18))
      chips.push(`<button class="resource-chip ${state.tag === t.tag ? "on" : ""}" data-tag="${esc(t.tag)}">${esc(t.tag)} ${t.count}</button>`);
    box.innerHTML = chips.join("");
    box.querySelectorAll("[data-tag]").forEach(b => b.onclick = () => {
      state.tag = b.dataset.tag || "";
      load();
    });
  }

  function renderList() {
    const box = root().querySelector("[data-list]");
    box.innerHTML = state.items.map(item => {
      const tags = (item.tags || []).map(t => `<span class="exp-ins-tag">${esc(t)}</span>`).join("");
      const sid = String(item.source_session || "");
      const known = sid.startsWith("sess-") && (S.sessions || []).some(s => s.session_id === sid);
      const source = sid
        ? (known
          ? `<button class="exp-source" data-open-session="${esc(sid)}" title="打开来源会话">${esc(item.source_title || sid)}</button>`
          : `<span class="exp-source muted">${esc(item.source_title || sid)}</span>`)
        : `<span class="exp-source muted">来源未记录</span>`;
      const iid = String(item.insight_id || "");
      return `<article class="exp-card">
        <div class="exp-card-tags">${tags || '<span class="exp-ins-tag muted">未打标签</span>'}</div>
        <p class="exp-insight">${esc(item.insight || "")}</p>
        ${item.context ? `<p class="exp-context">适用：${esc(item.context)}</p>` : ""}
        <footer>${source}<span class="exp-card-ops">
          <span class="exp-meta">${esc(item.source_model || "")} · ${esc(time(item.ts))}</span>
          ${iid ? `<button class="btn exp-del" data-del="${esc(iid)}" title="删除这条经验">删除</button>` : ""}
        </span></footer>
      </article>`;
    }).join("") || (state.q || state.tag ? "" : `
      <div class="resource-empty">
        暂无经验
        <p>会话结束后自动提炼，可在「提炼记录」查看处理进度。</p>
      </div>`);
    box.querySelectorAll("[data-open-session]").forEach(b =>
      b.onclick = () => openSession(b.dataset.openSession));
    box.querySelectorAll("[data-del]").forEach(b =>
      b.onclick = () => armConfirm(b, () => removeItem(b.dataset.del)));
    root().querySelector("[data-more]").hidden = !state.more;
  }

  /* 人工审核删除：本地就地移除并修正统计，服务端在系统审计留痕 */
  async function removeItem(id) {
    try {
      await api(`/api/experience/${encodeURIComponent(id)}`, {method: "DELETE"});
      state.items = state.items.filter(it => String(it.insight_id || "") !== id);
      state.offset = state.items.length;
      state.total = Math.max(0, (state.total || 1) - 1);
      if (state.stats) state.stats.total = state.total;
      renderTiles(state.stats || {});
      renderList();
      const status = root().querySelector("[data-status]");
      status.textContent = state.total
        ? `${state.total} 条经验`
        : (state.q || state.tag ? "没有匹配的经验，请更换关键词或标签。" : "");
      toast("已删除这条经验");
    } catch (e) { toast(e.message, true); }
  }

  function renderHistory(rows) {
    const box = root().querySelector("[data-hist]");
    const label = {ok: ["成功", "ok"], empty: ["无新增经验", "empty"], error: ["失败", "error"], skipped: ["跳过", "skipped"]};
    box.innerHTML = rows.map(h => {
      const [text, cls] = label[h.result] || [h.result || "未知", ""];
      const sid = String(h.session_id || "");
      const known = sid.startsWith("sess-") && (S.sessions || []).some(s => s.session_id === sid);
      return `<article class="exp-hist-row ${cls}">
        <header>
          <span class="exp-hist-badge">${esc(text)}</span>
          ${known ? `<button class="exp-source" data-open-session="${esc(sid)}">${esc(sid)}</button>` : `<span class="muted mono">${esc(sid)}</span>`}
          <small>${esc((h.ts || "").replace("T", " ").slice(0, 19))}</small>
        </header>
        <p>${esc(h.outcome || "")}${h.insights ? ` · 新增 ${h.insights} 条` : ""}${h.error ? ` · ${esc(h.error)}` : ""}</p>
      </article>`;
    }).join("") || `<p class="muted">暂无提炼记录，会话结束后可在这里查看。</p>`;
    box.querySelectorAll("[data-open-session]").forEach(b =>
      b.onclick = () => openSession(b.dataset.openSession));
  }

  function open() {
    shell();
    refreshSessions?.();
    load(true);
  }

  return {open, load};
})();
