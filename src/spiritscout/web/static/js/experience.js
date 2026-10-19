/* 经验提炼页：跨会话自我进化的全部产出与提炼过程记录。
   经验库是灵枢在真实任务收尾时提炼的实战洞察（非模板、非预置）——
   这里原样呈现学到的东西、从哪场会话学来、提炼是否成功。 */
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
          <span class="resource-eyebrow">自我进化 · Experience</span>
          <h2>经验提炼</h2>
          <p>每个会话收尾，灵枢从实战战报提炼可复用洞察。这里能看到它真实学到的一切——从哪场会话学来、提炼是否成功；提炼跑偏的条目（如混入单题解题步骤）可人工删除。</p>
        </div>
        <div class="resource-head-actions"><button class="btn" data-refresh>刷新</button></div>
      </header>
      <div class="exp-tiles" data-tiles></div>
      <div class="exp-toolbar">
        <label class="resource-search">检索学到的经验<input type="search" data-search placeholder="洞察 / 场景 / 标签…" maxlength="100"></label>
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
          <p class="exp-history-note">逐场会话的提炼尝试——成功、落空、失败都留痕，学没学到一目了然。</p>
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
        ? `${state.total} 条经验 · 全部提炼自真实会话`
        : (state.q || state.tag ? "没有匹配的经验——换个关键词或标签。" : "");
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
      <div class="exp-tile"><span>经验</span><b>${Number(data.total || 0).toLocaleString("zh-CN")}</b><small>当前过滤下的条数</small></div>
      <div class="exp-tile"><span>来源会话</span><b>${Number(data.sources || 0).toLocaleString("zh-CN")}</b><small>已沉淀经验的真实任务</small></div>
      <div class="exp-tile"><span>覆盖标签</span><b>${(data.tags || []).length}</b><small>场景检索维度</small></div>
      <div class="exp-tile"><span>最近提炼</span><b class="exp-tile-time">${esc(last)}</b><small>收尾自动进行</small></div>`;
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
          ? `<button class="exp-source" data-open-session="${esc(sid)}" title="打开来源会话">${esc(item.source_title || sid)} ↗</button>`
          : `<span class="exp-source muted">${esc(item.source_title || sid)}</span>`)
        : `<span class="exp-source muted">来源未记录</span>`;
      const iid = String(item.insight_id || "");
      return `<article class="exp-card">
        <div class="exp-card-tags">${tags || '<span class="exp-ins-tag muted">未打标签</span>'}</div>
        <p class="exp-insight">${esc(item.insight || "")}</p>
        ${item.context ? `<p class="exp-context">适用：${esc(item.context)}</p>` : ""}
        <footer>${source}<span class="exp-card-ops">
          <span class="exp-meta">${esc(item.source_model || "")} · ${esc(time(item.ts))}</span>
          ${iid ? `<button class="btn exp-del" data-del="${esc(iid)}" title="人工审核：删除这条经验">删除</button>` : ""}
        </span></footer>
      </article>`;
    }).join("") || (state.q || state.tag ? "" : `
      <div class="resource-empty">
        经验库还是空的
        <p>每个会话收尾时，灵枢会从战报中提炼 3~5 条实战洞察存入这里（被中止或失败的任务也会提炼教训）。跑完一个真实任务再回来看。</p>
        <p>右侧「提炼记录」可以看到每一次提炼尝试的结果。</p>
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
        ? `${state.total} 条经验 · 全部提炼自真实会话`
        : (state.q || state.tag ? "没有匹配的经验——换个关键词或标签。" : "");
      toast("已删除这条经验");
    } catch (e) { toast(e.message, true); }
  }

  function renderHistory(rows) {
    const box = root().querySelector("[data-hist]");
    const label = {ok: ["成功", "ok"], empty: ["无值得提炼", "empty"], error: ["失败", "error"], skipped: ["跳过", "skipped"]};
    box.innerHTML = rows.map(h => {
      const [text, cls] = label[h.result] || [h.result || "未知", ""];
      const sid = String(h.session_id || "");
      const known = sid.startsWith("sess-") && (S.sessions || []).some(s => s.session_id === sid);
      return `<article class="exp-hist-row ${cls}">
        <header>
          <span class="exp-hist-badge">${esc(text)}</span>
          ${known ? `<button class="exp-source" data-open-session="${esc(sid)}">${esc(sid)} ↗</button>` : `<span class="muted mono">${esc(sid)}</span>`}
          <small>${esc((h.ts || "").replace("T", " ").slice(0, 19))}</small>
        </header>
        <p>${esc(h.outcome || "")}${h.insights ? ` · 沉淀 ${h.insights} 条` : ""}${h.error ? ` · ${esc(h.error)}` : ""}</p>
      </article>`;
    }).join("") || `<p class="muted">还没有提炼记录——跑完第一个真实任务后出现。</p>`;
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
