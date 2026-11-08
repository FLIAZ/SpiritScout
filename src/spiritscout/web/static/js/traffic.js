"use strict";
/* 会话与全局流量页共用只读浏览器；离开、切会话时丢弃旧请求。 */
const TrafficPage = (() => {
  let active = null, generation = 0, timer = null;
  const coverage = I18n.t("这里只展示任务执行过程中记录的 HTTP/HTTPS、DNS、TCP 与会话代理交互。离线 PCAP 请在附件卡片中打开数据包预览。");
  const bytes = n => n < 1024 ? `${n || 0} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KiB` : `${(n / 1048576).toFixed(1)} MiB`;
  const when = n => n ? new Date(n * 1000).toLocaleString(I18n.dateLocale, {hour12:false}) : "—";
  function current(ctx) {
    return active === ctx && (ctx.global ? S.view === "traffic" :
      S.view === "session" && S.tab === "traffic" && S.cur === ctx.session);
  }
  function stop() {
    clearTimeout(timer); timer = null; ++generation; active?.proxyControls?.close(); active = null;
  }
  function stats(data) {
    return I18n.html`<div class="traffic-stats"><span><b>${data.total || 0}</b> 交互记录</span>
      <span><b>${data.errors || 0}</b> 失败 / 错误响应</span>
      <span><b>${bytes(data.request_bytes || 0)}</b> 请求体</span>
      <span><b>${bytes(data.response_bytes || 0)}</b> 已读取响应体</span></div>`;
  }
  function shell(ctx) {
    ctx.root.innerHTML = I18n.html`<div class="traffic-browser ${ctx.global ? "traffic-global" : ""}">
      ${ctx.global ? I18n.html('<nav class="traffic-projects" aria-label="按项目查看流量"></nav>') : ""}
      <div class="traffic-main">
        <header class="traffic-head"><div><h2>${ctx.global ? I18n.t("流量") : I18n.t("会话流量")}</h2><p class="muted">任务交互 · 按会话保存</p></div>
          <button class="btn sm" data-refresh>刷新</button></header>
        <p class="traffic-coverage">${coverage}</p>
        <div data-summary></div><div data-proxy-panel></div>
        ${ctx.global ? I18n.html('<section class="traffic-sessions" aria-label="会话流量统计"></section>') : ""}
        <section data-exchanges><div class="traffic-record-head"><h3 data-record-title>流量记录</h3><span class="muted" data-page-label></span></div>
        <form class="traffic-filter"><label>筛选请求<input name="query" type="search" maxlength="200" placeholder="地址或请求方法"></label>
          <label>方法<input name="method" maxlength="16" pattern="[A-Za-z]*" placeholder="GET / POST / DNS / TCP"></label>
          <label>状态码<input name="status" maxlength="3" pattern="[1-5]([0-9]{2}|xx)" placeholder="200 / 4xx"></label>
          <label>正文预览关键词<input name="body" maxlength="200" type="search" placeholder="支持中文与字面量子串"></label>
          <label class="traffic-checkbox"><input name="errors" type="checkbox">仅失败 / 错误响应</label><button class="btn sm">筛选</button></form>
        <p class="traffic-notice" role="status" data-message></p>
        <div class="traffic-table-wrap" data-records></div>
        <div class="traffic-pagination"><button class="btn sm" data-prev disabled>上一页</button><button class="btn sm" data-next disabled>下一页</button></div>
        <section class="traffic-detail" data-detail aria-label="请求响应详情" hidden></section></section>
      </div></div>`;
    ctx.root.querySelector("[data-refresh]").onclick = () => { reset(ctx); refresh(ctx); };
    ctx.root.querySelector(".traffic-filter").onsubmit = event => {
      event.preventDefault();
      ctx.query = ctx.root.querySelector('[name="query"]').value.trim();
      ctx.errors = ctx.root.querySelector('[name="errors"]').checked;
      ctx.method = ctx.root.querySelector('[name="method"]').value.trim();
      ctx.status = ctx.root.querySelector('[name="status"]').value.trim();
      ctx.body = ctx.root.querySelector('[name="body"]').value.trim();
      reset(ctx); loadRows(ctx);
    };
    ctx.root.querySelector("[data-prev]").onclick = () => {
      if (!ctx.history.length) return;
      ctx.before = ctx.history.pop(); clearDetail(ctx); loadRows(ctx);
    };
    ctx.root.querySelector("[data-next]").onclick = () => {
      if (!ctx.next) return;
      ctx.history.push(ctx.before); ctx.before = ctx.next; clearDetail(ctx); loadRows(ctx);
    };
  }
  function selectRecord(ctx, id) {
    ctx.selectedRecord = id;
    ctx.root.querySelectorAll("[data-traffic-row]").forEach(row => {
      const selected = String(row.dataset.trafficRow) === String(id);
      row.classList.toggle("traffic-record-selected", selected); row.setAttribute("aria-selected", String(selected));
    });
  }
  function clearDetail(ctx) {
    selectRecord(ctx, null);
    ++ctx.detailVersion;
    const box = ctx.root.querySelector("[data-detail]");
    box.hidden = true; box.replaceChildren();
  }
  function reset(ctx) {
    ctx.before = null; ctx.history = []; ctx.next = null;
    clearDetail(ctx);
  }
  function message(ctx, text, error = false) {
    const box = ctx.root.querySelector("[data-message]");
    box.textContent = text; box.classList.toggle("traffic-error", error);
  }
  function renderCatalog(ctx) {
    const groups = ctx.catalog.projects;
    if (!groups.some(g => g.project_id === ctx.project)) ctx.project = "";
    const group = groups.find(g => g.project_id === ctx.project);
    ctx.root.querySelector("[data-summary]").innerHTML = stats(group);
    const nav = ctx.root.querySelector(".traffic-projects");
    nav.innerHTML = I18n.html`<h3>项目</h3>` + groups.map(g => I18n.html`<button type="button" data-project="${esc(g.project_id)}"
      aria-pressed="${g.project_id === ctx.project}"><svg class="traffic-project-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="M3 7.5A2.5 2.5 0 0 1 5.5 5h3.4l2 2h7.6A2.5 2.5 0 0 1 21 9.5v7A2.5 2.5 0 0 1 18.5 19h-13A2.5 2.5 0 0 1 3 16.5z"/></svg>
      <span class="traffic-project-name">${esc(["*", ""].includes(g.project_id) ? I18n.t(g.name) : g.name)}</span><small>${g.session_count} 个会话 · ${g.total} 条交互</small></button>`).join("");
    nav.querySelectorAll("button").forEach(button => button.onclick = () => {
      ctx.project = button.dataset.project; ctx.session = null; reset(ctx); renderCatalog(ctx); loadRows(ctx);
    });
    const sessions = ctx.catalog.sessions.filter(s => ctx.project === "*" || s.project_id === ctx.project);
    if (!sessions.some(s => s.session_id === ctx.session)) {
      ctx.session = (sessions.find(s => s.total) || sessions[0])?.session_id || null;
      reset(ctx);
    }
    const box = ctx.root.querySelector(".traffic-sessions");
    const html = I18n.html`<h3>${esc(["*", ""].includes(group.project_id) ? I18n.t(group.name) : group.name)} · 会话统计</h3>` + (sessions.length ? I18n.html`<div class="traffic-table-wrap"><table>
      <thead><tr><th>会话</th><th>交互记录</th><th>失败 / 错误响应</th><th>响应体</th></tr></thead><tbody>${sessions.map(s => `<tr>
        <td><button class="traffic-link" data-session="${esc(s.session_id)}" aria-pressed="false">${esc(s.title || s.session_id)}</button>${s.available ? "" : I18n.html('<span class="traffic-error"> 暂不可读</span>')}</td>
        <td>${s.total}</td><td>${s.errors}</td><td>${bytes(s.response_bytes)}</td></tr>`).join("")}</tbody></table></div>` : I18n.html('<p class="muted">此项目还没有会话。</p>'));
    // 选中态单独更新，避免点击或相同数据轮询时替换滚动容器。
    if (ctx.sessionListHTML !== html || ctx.sessionListProject !== ctx.project) {
      const previous = box.querySelector(".traffic-table-wrap");
      const keepPosition = ctx.sessionListProject === ctx.project && previous;
      const top = previous?.scrollTop || 0, left = previous?.scrollLeft || 0;
      const focusedSession = keepPosition && previous.contains(document.activeElement)
        ? document.activeElement.dataset.session : null;
      box.innerHTML = html;
      ctx.sessionListHTML = html; ctx.sessionListProject = ctx.project;
      const list = box.querySelector(".traffic-table-wrap");
      if (keepPosition && list) { list.scrollTop = top; list.scrollLeft = left; }
      box.querySelectorAll("[data-session]").forEach(button => {
        button.onclick = () => {
          ctx.session = button.dataset.session; reset(ctx); renderCatalog(ctx); loadRows(ctx);
        };
        if (button.dataset.session === focusedSession) button.focus({preventScroll:true});
      });
    }
    box.querySelectorAll("[data-session]").forEach(button => {
      const selected = button.dataset.session === ctx.session;
      button.setAttribute("aria-pressed", String(selected));
      button.closest("tr").classList.toggle("traffic-selected", selected);
    });
    const chosen = sessions.find(s => s.session_id === ctx.session);
    ctx.root.querySelector("[data-record-title]").textContent = chosen ? I18n.t`${chosen.title || chosen.session_id} · 流量记录` : I18n.t("流量记录");
  }
  async function refresh(ctx) {
    if (!current(ctx)) return;
    const version = ++ctx.catalogVersion;
    ++ctx.refreshing;
    try {
      if (ctx.global) {
        const data = await api("/api/traffic");
        if (!current(ctx) || version !== ctx.catalogVersion) return;
        ctx.catalog = data; renderCatalog(ctx);
      }
      await loadRows(ctx);
    } catch (error) {
      if (current(ctx) && version === ctx.catalogVersion) message(ctx, I18n.t`流量读取失败：${error.message}。可点击刷新重试。`, true);
    } finally {
      --ctx.refreshing;
    }
  }
  async function loadRows(ctx) {
    if (!current(ctx)) return;
    const version = ++ctx.rowsVersion, session = ctx.session;
    if (typeof TrafficProxy !== "undefined") {
      if (ctx.proxyControls?.session !== session) { ctx.proxyControls?.close(); ctx.proxyControls = null; }
      if (session && !ctx.proxyControls) ctx.proxyControls = TrafficProxy.mount(ctx.root.querySelector("[data-proxy-panel]"), session);
      if (ctx.proxyControls) await ctx.proxyControls.refresh();
      if (!current(ctx) || version !== ctx.rowsVersion) return;
    }
    const records = ctx.root.querySelector("[data-records]");
    // 切会话立刻清空上一会话的行，不在网络等待期间显示错误归属。
    if (ctx.displayedSession !== session) { records.replaceChildren(); ctx.displayedSession = session; }
    ctx.root.querySelector("[data-prev]").disabled = true;
    ctx.root.querySelector("[data-next]").disabled = true;
    if (!session) {
      records.replaceChildren(); ctx.root.querySelector("[data-page-label]").textContent = "";
      message(ctx, I18n.t("选择一个会话后查看流量记录。")); return;
    }
    message(ctx, I18n.t("正在读取流量…"));
    const params = new URLSearchParams({limit:"50", query:ctx.query, errors_only:String(ctx.errors), method:ctx.method, status:ctx.status, body:ctx.body});
    if (ctx.before) params.set("before", ctx.before);
    ++ctx.rowLoading;
    try {
      const data = await api(`/api/sessions/${encodeURIComponent(session)}/traffic?${params}`);
      if (!current(ctx) || version !== ctx.rowsVersion || ctx.session !== session) return;
      if ((ctx.method || ctx.status || ctx.body) && !data.filters) {
        records.replaceChildren(); clearDetail(ctx); ctx.next = null;
        ctx.root.querySelector("[data-page-label]").textContent = I18n.t("筛选未应用");
        message(ctx, I18n.t("当前后端尚不支持方法、状态码和正文筛选，不能将未筛选结果作为匹配结果。请在任务结束后重启后端并刷新页面。"), true);
        return;
      }
      if (!ctx.global) ctx.root.querySelector("[data-summary]").innerHTML = stats(data.summary);
      ctx.next = data.has_more ? data.next_before : null;
      ctx.root.querySelector("[data-page-label]").textContent = I18n.t`第 ${ctx.history.length + 1} 页 · ${data.items.length} 条`;
      ctx.root.querySelector("[data-prev]").disabled = !ctx.history.length;
      ctx.root.querySelector("[data-next]").disabled = !ctx.next;
      records.innerHTML = data.items.length ? I18n.html`<table><thead><tr><th>序号 / 时间</th><th>方法 / 地址</th><th>状态</th><th>耗时</th><th>响应体</th><th>Agent</th></tr></thead>
        <tbody>${data.items.map(row => `<tr data-traffic-row="${row.id}"${String(ctx.selectedRecord) === String(row.id) ? ' class="traffic-record-selected" aria-selected="true"' : ' aria-selected="false"'}><td class="traffic-time"><b>#${row.id}</b><br>${esc(when(row.captured_at))}</td>
          <td><span class="traffic-method">${esc(row.method)}</span><button class="traffic-link traffic-url" data-record="${row.id}">${esc(row.url)}</button></td>
          <td class="${row.error || row.status >= 400 ? "traffic-error" : ""}">${esc(row.error || row.status || "—")}</td>
          <td>${Math.round(row.duration_ms)} ms</td><td>${bytes(row.response_bytes)}</td><td>${esc(row.agent_id || "—")}</td></tr>`).join("")}</tbody></table>` : "";
      records.querySelectorAll("[data-record]").forEach(button => button.onclick = () => loadDetail(ctx, button.dataset.record));
      message(ctx, data.items.length ? I18n.t("点击地址查看请求与响应。") : ctx.query || ctx.errors || ctx.method || ctx.status || ctx.body ? I18n.t("没有符合筛选条件的流量。") : I18n.t("尚无流量记录。新产生的内置 HTTP/HTTPS、DNS 和 TCP 观测会显示在这里；历史会话不会补采。"));
    } catch (error) {
      if (current(ctx) && version === ctx.rowsVersion) message(ctx, I18n.t`流量读取失败：${error.message}。可点击刷新重试。`, true);
    } finally {
      --ctx.rowLoading;
    }
  }
  async function loadDetail(ctx, id) {
    selectRecord(ctx, id);
    const session = ctx.session, version = ++ctx.detailVersion;
    const box = ctx.root.querySelector("[data-detail]");
    box.hidden = false; box.textContent = I18n.t("正在读取请求与响应…");
    try {
      const row = await api(`/api/sessions/${encodeURIComponent(session)}/traffic/${id}`);
      if (!current(ctx) || ctx.session !== session || version !== ctx.detailVersion) return;
      const headers = items => (items || []).map(([k, v]) => `${k}: ${v}`).join("\n") || I18n.t("（无）");
      if (row.protocol && row.protocol !== "HTTP") {
        box.innerHTML = I18n.html`<header class="traffic-head"><h3>${esc(row.protocol)} 观测 #${row.id}</h3><button class="btn sm" data-close>收起详情</button></header>
          <p class="traffic-detail-url">${esc(row.url)}</p><p class="muted">${esc(row.note)}</p>
          <pre class="traffic-observation">${esc(JSON.stringify(row.observations, null, 2))}</pre>
          ${row.error ? `<p class="traffic-error">${esc(row.error)}</p>` : ""}
          <h4>已读取横幅 / 数据</h4><pre class="traffic-observation">${esc(row.response_body.text || I18n.t("（未读取到数据）"))}</pre>`;
        box.querySelector("[data-close]").onclick = () => clearDetail(ctx); box.scrollIntoView({block:"nearest"}); return;
      }
      const body = value => `<p class="muted">${esc(value.encoding)}${value.truncated ? I18n.t(" · 预览已截断 / 省略") : ""}</p><pre>${esc(value.text || I18n.t("（空正文）"))}</pre>`;
      box.innerHTML = I18n.html`<header class="traffic-head"><h3>流量 #${row.id}</h3><button class="btn sm" data-close>收起详情</button></header>
        <p class="traffic-detail-url">${esc(row.method)} ${esc(row.url)}</p>
        <p class="muted">${esc(when(row.captured_at))} · 来源 ${esc(row.source)} · 任务 ${esc(row.task_id || "—")} · 调用 ${esc(row.invocation_id || "—")}</p>
        <p class="traffic-coverage">常见认证头、认证参数及 JSON / 表单敏感字段已隐藏；其他正文仍可能含业务数据。响应体为解压后的采集内容。</p>
        ${row.original_available ? I18n.html`<div class="traffic-original-links"><a class="btn sm" href="/api/sessions/${encodeURIComponent(session)}/traffic/${row.id}/original?direction=request">下载请求原文</a><a class="btn sm" href="/api/sessions/${encodeURIComponent(session)}/traffic/${row.id}/original?direction=response">下载响应原文</a><a class="btn sm" href="/api/sessions/${encodeURIComponent(session)}/traffic/${row.id}/original?direction=metadata">下载原始头与证据元信息</a></div><p class="muted">代理原文未脱敏，并保留内容编码；每个方向最多保存 32 MiB，达到上限不截断目标流量。${row.transport === "WS" ? I18n.t("此记录包含 WebSocket 帧字节流。") : ""}</p>` : ""}
        ${row.capture_error ? I18n.html`<p class="traffic-error">正文保存不完整：${esc(row.capture_error)}</p>` : ""}
        ${row.error ? I18n.html`<p class="traffic-error">请求失败：${esc(row.error)}</p>` : ""}
        <div class="traffic-pair"><section><h4>请求头</h4><pre>${esc(headers(row.request_headers))}</pre><h4>请求体</h4>${body(row.request_body)}</section>
        <section><h4>响应头 · ${esc(row.status || I18n.t("未收到响应"))} ${esc(row.http_version)}</h4><pre>${esc(headers(row.response_headers))}</pre>
        <h4>响应体</h4><p class="muted">${row.response_complete ? I18n.t("已读取完整响应体") : I18n.t("响应体未完整采集（可能为重定向、下载上限或请求失败）")}</p>${body(row.response_body)}</section></div>`;
      box.querySelector("[data-close]").onclick = () => clearDetail(ctx);
      box.scrollIntoView({block:"nearest"});
    } catch (error) {
      if (current(ctx) && ctx.session === session && version === ctx.detailVersion) box.textContent = I18n.t`详情读取失败：${error.message}`;
    }
  }
  function open(global) {
    stop();
    const ctx = {root: $(global ? "#view-traffic" : "#tab-traffic"), global,
      session: global ? null : S.cur, project:"*", selectedRecord:null, proxyControls:null, query:"", method:"", status:"", body:"", errors:false, before:null,
      history:[], next:null, rowsVersion:0, catalogVersion:0, detailVersion:0, refreshing:0, rowLoading:0};
    active = ctx; shell(ctx); refresh(ctx);
    const token = generation;
    const tick = async () => {
      if (!current(ctx) || token !== generation) return;
      if (document.visibilityState === "visible" && !ctx.before && !ctx.refreshing && !ctx.rowLoading) await refresh(ctx);
      if (current(ctx) && token === generation) timer = setTimeout(tick, 5000);
    };
    timer = setTimeout(tick, 5000);
  }
  return {
    navigate(view) { if (view === "traffic") open(true); else stop(); },
    sessionTab() { if (S.view === "session" && S.tab === "traffic") open(false); else stop(); },
  };
})();
