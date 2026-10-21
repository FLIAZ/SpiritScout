/* 项目分区：用户创建的共享记忆分区，不是授权边界。
   项目页只做归属与历史浏览；测试边界由各会话模型结合用户原文上下文维护。 */
const ProjectsPage = (() => {
  const ALL = "*";
  let catalog = null;                 // {projects, unclassified, all}
  let current = null;                 // null=总览；""=未分类；"*"=全部；否则项目 ID
  let tab = "assets";                 // assets | findings | sessions
  let detail = { gen: 0, q: "", items: [], shown: 0, total: 0, more: false };
  let poll = null;

  const root = () => $("#view-projects");
  const time = t => t ? new Date(t * 1000).toLocaleString("zh-CN", {hour12: false}) : "未记录";
  const KIND_CN = {endpoint: "接口", app: "应用页面", url: "网址", domain: "域名",
                   host: "主机", ip: "IP", service: "服务端口", finding: "历史发现"};

  function folder(id) {
    if (id === ALL) return (catalog && catalog.all) || null;
    if (id === "") return (catalog && catalog.unclassified) || null;
    return ((catalog && catalog.projects) || []).find(p => p.project_id === id) || null;
  }

  /* 纯渲染函数（项目页与 tests/test_project_folders.js 共用）：
     项目卡片 + 恒在的未分类与全部；不读取 DOM，不绑定事件。 */
  function projectCardsHtml(cat, active) {
    const clock = t => t ? new Date(t * 1000).toLocaleString("zh-CN", {hour12: false}) : "未记录";
    const icon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 7.5A2.5 2.5 0 0 1 5.5 5h3.4l2 2h7.6A2.5 2.5 0 0 1 21 9.5v7A2.5 2.5 0 0 1 18.5 19h-13A2.5 2.5 0 0 1 3 16.5z"/></svg>';
    const card = (p, id, extra) => {
      const selected = active !== null && active !== undefined && String(active) === String(id);
      const name = p.name || (id === "" ? "未分类" : "全部");
      const raw = p.description || (id === ALL
        ? "所有项目与未分类的汇总"
        : id === "" ? "未加入项目的会话与资产会自动归到这里" : "尚无说明");
      const desc = raw.length > 140 ? raw.slice(0, 140).trimEnd() + "…" : raw;
      const badge = p.read_only
        ? `<span class="pf-ro">${id === ALL ? "只读总览" : "自动归档"}</span>` : "";
      const counts = `${Number(p.session_count || 0)} 会话 · `
        + `${Number(p.asset_count || 0)} 资产 · ${Number(p.finding_count || 0)} 发现`;
      return `<button type="button" class="pfolder${extra || ""}${selected ? " on" : ""}" data-folder="${esc(id)}">`
        + `<span class="pf-top">${icon}${badge}</span>`
        + `<b class="pf-name">${esc(name)}</b>`
        + `<span class="pf-desc">${esc(desc)}</span>`
        + `<span class="pf-count">${counts}</span>`
        + `<span class="pf-time">最近活动 ${esc(clock(p.last_seen))}</span></button>`;
    };
    const cards = ((cat && cat.projects) || []).map(p => card(p, p.project_id || ""));
    if (cat && cat.unclassified) cards.push(card(cat.unclassified, "", " dashed"));
    if (cat && cat.all) cards.push(card(cat.all, ALL, " all"));
    return cards.join("");
  }

  function statsHtml(f) {
    return `<span><b>${Number(f.session_count || 0)}</b> 会话</span>`
      + `<span><b>${Number(f.asset_count || 0)}</b> 资产</span>`
      + `<span><b>${Number(f.finding_count || 0)}</b> 发现</span>`
      + `<span>最近活动 ${esc(time(f.last_seen))}</span>`;
  }

  function shell() {
    const el = root();
    if (!el || el.dataset.ready) return;
    el.dataset.ready = "yes";
    el.innerHTML = `<header class="resource-head projects-head">
      <div><span class="resource-eyebrow">记忆分区</span><h2>项目</h2>
        <p>按项目归档会话、资产与发现；同项目的会话共享历史记忆。项目不改变授权范围，历史记录仍需复核。</p></div>
      <div class="resource-head-actions">
        <button class="btn" data-refresh type="button">刷新</button>
        <button class="btn primary" data-new type="button">+ 新建项目</button>
      </div>
    </header>
    <div class="projects-grid" id="projects-grid"></div>
    <div class="project-detail" id="project-detail" hidden></div>`;
    el.querySelector(".projects-head [data-refresh]").onclick = () =>
      loadCatalog().catch(e => toast(e.message, true));
    el.querySelector("[data-new]").onclick = () => editDialog(null);
  }

  function renderGrid() {
    const grid = $("#projects-grid"), box = $("#project-detail");
    if (!grid || !box) return;
    if (current !== null) { grid.hidden = true; box.hidden = false; return; }
    grid.hidden = false; box.hidden = true;
    grid.innerHTML = projectCardsHtml(catalog, current);
    grid.querySelectorAll("[data-folder]").forEach(card =>
      card.onclick = () => openFolder(card.dataset.folder));
  }

  function openFolder(id) {
    current = id;
    tab = "assets";
    detail = {gen: detail.gen + 1, q: "", items: [], shown: 0, total: 0, more: false};
    renderGrid();
    renderDetail();
  }
  function backToGrid() {
    current = null;
    renderGrid();
  }

  function renderDetail() {
    const box = $("#project-detail");
    if (!box) return;
    const f = folder(current);
    if (!f) {
      current = null; renderGrid(); return;
    }
    const readonly = !!f.read_only;
    const kicker = current === ALL ? "全库汇总" : current === "" ? "未分类" : "项目分区";
    const desc = f.description || (current === ALL
      ? "跨项目的只读总览；每个项目只展示自己的最新观测，互不覆盖。"
      : current === "" ? "没有加入项目的会话与资产会自动归到这里。"
      : "尚无说明。点击「编辑」写入本项目的测试规则，后续同项目会话会直接读到。");
    box.innerHTML = `<div class="pd-head">
      <button class="btn sm" type="button" data-back>← 项目总览</button>
      <div class="pd-title">
        <div><span class="resource-eyebrow">${kicker}</span><h3>${esc(f.name || "未分类")}</h3>
          <p class="pd-desc">${esc(desc)}</p></div>
        <div class="pd-ops">${readonly ? "" :
          `<button class="btn sm" type="button" data-rename>重命名</button>
           <button class="btn sm" type="button" data-edit-desc title="修改项目说明与测试规则">编辑说明</button>
           <button class="btn sm danger" type="button" data-delete>删除</button>`}</div>
      </div>
      <div class="pd-stats">${statsHtml(f)}</div>
    </div>
    <div class="pd-toolbar">
      <div class="pd-tabs" role="tablist" aria-label="项目内容">
        ${[["assets", "资产"], ["findings", "发现"], ["sessions", "会话"]].map(([key, name]) =>
          `<button type="button" role="tab" aria-selected="${tab === key}" data-ptab="${key}" class="${tab === key ? "on" : ""}">${name}</button>`).join("")}
      </div>
      <label class="pd-search"><span class="sr-only">检索</span>
        <input type="search" data-pq maxlength="200" placeholder="按名称、地址或类型检索"></label>
      <span class="pd-count" data-pcount role="status"></span>
      <button class="btn sm" type="button" data-refresh>刷新</button>
    </div>
    <p class="pd-note">历史记录均未在本次会话复核，不构成授权，也不代表漏洞或资产现状。点击记录可查看来源时间线与所属项目。</p>
    <div class="pd-body" data-body><div class="resource-empty">正在读取…</div></div>
    <button class="btn resource-more" type="button" data-more hidden>加载更多</button>`;
    bindDetail();
    loadList();
  }

  function bindDetail() {
    const box = $("#project-detail");
    box.querySelector("[data-back]").onclick = backToGrid;
    const search = box.querySelector("[data-pq]");
    search.value = detail.q;
    let timer;
    search.oninput = () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        detail.q = search.value.trim();
        loadList();
      }, 250);
    };
    box.querySelector(".pd-toolbar [data-refresh]").onclick = () => {
      loadCatalog().then(() => { if (current !== null) renderDetail(); })
        .catch(e => toast(e.message, true));
    };
    box.querySelector("[data-more]").onclick = () => loadList(true);
    box.querySelectorAll("[data-ptab]").forEach(button => button.onclick = () => {
      tab = button.dataset.ptab;
      renderDetail();
    });
    const rename = box.querySelector("[data-rename]");
    if (rename) rename.onclick = () => editDialog(folder(current));
    const editDesc = box.querySelector("[data-edit-desc]");
    if (editDesc) editDesc.onclick = () => editDialog(folder(current), "desc");
    const remove = box.querySelector("[data-delete]");
    if (remove) remove.onclick = () => deleteDialog(folder(current));
  }

  function updateCount() {
    const el = $("#project-detail [data-pcount]");
    if (!el) return;
    el.textContent = detail.total ? `${detail.shown} / ${detail.total}` : "";
  }

  async function loadList(append = false) {
    const body = $("#project-detail [data-body]");
    if (!body) return;
    const generation = ++detail.gen;
    if (tab === "sessions") {
      detail.items = []; detail.more = false;
      renderSessionsTab();
      return;
    }
    if (!append) {
      detail.items = []; detail.shown = 0; detail.total = 0; detail.more = false;
      body.innerHTML = '<div class="resource-empty">正在读取…</div>';
      updateCount();
    }
    const params = new URLSearchParams({
      q: detail.q, offset: String(append ? detail.items.length : 0), limit: "40",
      kind: tab === "findings" ? "finding" : "asset"});
    if (current === "") params.set("unclassified", "1");
    else if (current !== ALL) params.set("project", current);
    try {
      const data = await api("/api/assets?" + params.toString());
      if (generation !== detail.gen || current === null) return;
      detail.items = append ? [...detail.items, ...(data.items || [])] : (data.items || []);
      detail.total = Number(data.total ?? detail.items.length);
      detail.more = !!data.has_more;
      renderList();
    } catch (e) {
      if (generation !== detail.gen) return;
      body.innerHTML = `<div class="resource-empty">读取失败：${esc(e.message)}</div>`;
    }
  }

  function assetRow(item, index) {
    const d = item.data || {};
    const finding = d.asset_type === "finding" || item.kind === "finding";
    const fp = d.fingerprint || {};
    const title = finding ? (fp.title || d.value || item.identity) : (d.value || item.identity);
    const label = finding
      ? [fp.vuln_type, fp.severity && `危害 ${fp.severity}`].filter(Boolean).join(" · ") || "历史发现"
      : (KIND_CN[d.asset_type] || KIND_CN[item.kind] || item.kind || "资产");
    const badge = item.changed ? "观测有变化" : item.freshness === "recent" ? "近期观测" : "建议复核";
    return `<button type="button" class="pd-row" data-asset="${index}">
      <span class="pd-row-top"><b>${esc(title || "")}</b>
        <span class="resource-badge ${item.changed ? "changed" : ""}">${esc(badge)}</span></span>
      <span class="pd-row-sub">${esc(label)} · ${Number(item.observations || 0)} 次观测 · ${esc(time(item.last_seen))}</span>
      <span class="pd-row-src">${esc(item.source || "")}</span></button>`;
  }

  function renderList() {
    const body = $("#project-detail [data-body]");
    if (!body) return;
    detail.shown = detail.items.length;
    if (!detail.items.length) {
      body.innerHTML = `<div class="resource-empty">${detail.q ? "没有匹配记录" : "这里还没有记录"}`
        + `<p>${tab === "findings" ? "只有通过复核并确认收录的发现才会进入共享目录。" : "运行会归入此项目的会话后，记录会自动出现。"}</p></div>`;
    } else {
      body.innerHTML = `<div class="pd-list">${detail.items.map(assetRow).join("")}</div>`;
      body.querySelectorAll("[data-asset]").forEach(button =>
        button.onclick = () => loadHistory(detail.items[+button.dataset.asset]));
    }
    const more = $("#project-detail [data-more]");
    if (more) more.hidden = !detail.more;
    updateCount();
  }

  function sessionMatches(session) {
    if (current === ALL) return true;
    const projects = (catalog && catalog.projects) || [];
    if (current === "") {
      return !session.project_id || !projects.some(p => p.project_id === session.project_id);
    }
    return session.project_id === current;
  }

  function renderSessionsTab() {
    const body = $("#project-detail [data-body]");
    if (!body) return;
    const q = detail.q.toLowerCase();
    const list = (S.sessions || []).filter(sessionMatches).filter(session =>
      !q || JSON.stringify([session.title, session.target, session.session_id])
        .toLowerCase().includes(q));
    detail.shown = list.length; detail.total = list.length;
    if (!list.length) {
      body.innerHTML = `<div class="resource-empty">${q ? "没有匹配的会话" : "这个分区还没有会话"}`
        + `<p>在任务页下达任务时选择此项目，会话就会归到这里。</p></div>`;
    } else {
      body.innerHTML = `<div class="pd-list">${list.map(session => `
        <button type="button" class="pd-row" data-session="${esc(session.session_id)}">
          <span class="pd-row-top"><b>${esc(session.title || session.target || session.session_id)}</b>
            <span class="pill ${esc(session.status)}">${esc(STATUS_CN[session.status] || session.status)}</span></span>
          <span class="pd-row-sub">${esc(MODE_CN[session.mode] || session.mode || "")} · ${esc(projectName(session.project_id))} · ${esc(session.session_id)}</span>
        </button>`).join("")}</div>`;
      body.querySelectorAll("[data-session]").forEach(button =>
        button.onclick = () => openSession(button.dataset.session));
    }
    const more = $("#project-detail [data-more]");
    if (more) more.hidden = true;
    updateCount();
  }

  async function loadHistory(item) {
    const body = $("#project-detail [data-body]");
    if (!body) return;
    const generation = ++detail.gen;
    body.innerHTML = '<div class="resource-empty">正在读取观测历史…</div>';
    try {
      const data = await api(`/api/assets/${encodeURIComponent(item.id)}/history`);
      if (generation !== detail.gen || current === null) return;
      const d = item.data || {}, fp = d.fingerprint || {};
      const title = (d.asset_type === "finding" ? (fp.title || d.value) : d.value) || item.identity || "";
      body.innerHTML = `<div class="pd-history">
        <button class="btn sm" type="button" data-backlist>← 返回列表</button>
        <div class="pd-hist-head"><span class="resource-eyebrow">观测档案</span>
          <h3>${esc(title)}</h3>
          <p>${esc(item.identity || item.id)} · 最近观测 ${esc(time(item.last_seen))}</p>
          <p class="resource-history-note">这些是历史记录。资产、账号权限和内容可能变化，不能视为本次已验证；也不构成授权。</p>
          <button class="btn" type="button" data-recheck>用新会话复核此资产 ↗</button></div>
        <div class="asset-observations">${(data.items || []).map((entry, index) => `
          <article><span class="asset-time">${esc(time(entry.observed_at))}${index === 0 ? " · 最近观测" : ""}
            · ${esc(projectName(entry.project_id))}</span>
            <small>${esc(entry.source)}</small>
            <pre>${esc(JSON.stringify(entry.data, null, 2))}</pre></article>`).join("")
          || '<div class="resource-empty">暂无观测记录</div>'}</div>
        ${data.has_more ? '<p class="muted">当前显示最近 30 条观测。</p>' : ""}
      </div>`;
      body.querySelector("[data-backlist]").onclick = () => renderDetail();
      body.querySelector("[data-recheck]").onclick = () => {
        const project = current === ALL ? "" : current;
        fillSelect($("#c-project"), project);
        show("home");
        $("#c-mission").value = `复核 ${title} 的当前状态。先读取已有资产观测，对比变化；历史结果仅供参考。请依据本次提供的授权开展。`;
        if (typeof homeSync === "function") homeSync();
        $("#c-mission").focus();
      };
    } catch (e) {
      if (generation === detail.gen) body.innerHTML = `<div class="resource-empty">历史读取失败：${esc(e.message)}</div>`;
    }
  }

  /* 下拉选项：未分类恒在；选中值不在目录里时回退未分类。
     选项未变化不重建 DOM，避免轮询打断已展开的 .selx 面板。 */
  function fillSelect(select, selected = "") {
    if (!select) return;
    const projects = (catalog && catalog.projects) || [];
    const signature = projects.map(p => p.project_id + "\u0000" + p.name).join("\u0001");
    if (select.dataset.pjSignature !== signature) {
      select.dataset.pjSignature = signature;
      select.innerHTML = '<option value="">未分类</option>'
        + projects.map(p => `<option value="${esc(p.project_id)}">${esc(p.name)}</option>`).join("");
    }
    select.value = projects.some(p => p.project_id === selected) ? selected : "";
    if (select.closest(".selx")) { selxRebuild(select); selxSync(select); }
  }

  function editDialog(project, focus = "name") {
    const creating = !project;
    const dlg = document.createElement("dialog");
    dlg.className = "project-dlg";
    dlg.innerHTML = `<h3 style="margin:0">${creating ? "新建项目" : "编辑项目"}</h3>
      <p class="muted" style="font-size:12px;margin:10px 0 0;line-height:1.75">
        项目是会话与资产记忆的分区，不是授权边界。说明可以写完整的测试规则
        （平台限制、禁测项、提交要求、已知坑等），同项目的后续会话会直接读到；
        规则不会扩大任何任务的授权范围。</p>
      <div class="dlg-grid">
        <label class="wide"><span>项目名 *</span>
          <input data-pj-name maxlength="80" placeholder="例如：bilibili SRC"></label>
        <label class="wide"><span>项目说明与测试规则（可选，不限长度）</span>
          <textarea data-pj-desc rows="10" spellcheck="false"
            placeholder="例：只测主站与直播域；RPS 不超过 5；禁止爆破与 DoS；发现仅提交可复现证据；账号使用测试号……"></textarea></label>
      </div>
      <div class="pj-error" role="alert"></div>
      <div class="dlg-ops"><button type="button" class="btn" data-cancel>取消</button>
        <button type="button" class="btn primary" data-ok>${creating ? "创建" : "保存"}</button></div>`;
    document.body.append(dlg);
    dlg.querySelector("[data-pj-name]").value = project?.name || "";
    dlg.querySelector("[data-pj-desc]").value = project?.description || "";
    const close = () => { dlg.close(); dlg.remove(); };
    dlg.querySelector("[data-cancel]").onclick = close;
    dlg.addEventListener("cancel", close);
    dlg.querySelector("[data-ok]").onclick = async () => {
      const name = dlg.querySelector("[data-pj-name]").value.trim();
      const description = dlg.querySelector("[data-pj-desc]").value.trim();
      if (!name) { dlg.querySelector("[data-pj-name]").focus(); return; }
      const button = dlg.querySelector("[data-ok]");
      button.disabled = true;
      try {
        const saved = creating
          ? await api("/api/projects", {method: "POST", body: JSON.stringify({name, description})})
          : await api(`/api/projects/${encodeURIComponent(project.project_id)}`,
              {method: "PATCH", body: JSON.stringify({name, description})});
        close();
        toast(creating ? "项目已创建" : "项目已更新");
        await loadCatalog(true);
        if (creating) {
          fillSelect($("#c-project"), saved.project_id);
          if (S.view === "projects") openFolder(saved.project_id);
        } else if (S.view === "projects" && current === project.project_id) {
          renderDetail();
        }
      } catch (e) {
        dlg.querySelector(".pj-error").textContent = e.message;
        button.disabled = false;
      }
    };
    dlg.showModal();
    const target = focus === "desc" && !creating
      ? dlg.querySelector("[data-pj-desc]") : dlg.querySelector("[data-pj-name]");
    target.focus();
    if (focus === "desc" && !creating) target.setSelectionRange(target.value.length,
                                                              target.value.length);
  }

  function deleteDialog(project) {
    const dlg = document.createElement("dialog");
    dlg.className = "project-dlg";
    dlg.innerHTML = `<h3 style="margin:0">删除项目「${esc(project.name)}」</h3>
      <p style="font-size:12.5px;line-height:1.85;margin:12px 0 0">
        只解除分区关系，<b>不会删除任何资产、观测或发现</b>：项目内的资产与发现转为未分类，
        项目内的 ${Number(project.session_count || 0)} 个会话移出项目。</p>
      <div class="pj-error" role="alert"></div>
      <div class="dlg-ops" style="margin-top:16px"><button type="button" class="btn" data-cancel>取消</button>
        <button type="button" class="btn danger" data-ok>删除项目</button></div>`;
    document.body.append(dlg);
    const close = () => { dlg.close(); dlg.remove(); };
    dlg.querySelector("[data-cancel]").onclick = close;
    dlg.addEventListener("cancel", close);
    dlg.querySelector("[data-ok]").onclick = async () => {
      const button = dlg.querySelector("[data-ok]");
      button.disabled = true;
      try {
        const result = await api(`/api/projects/${encodeURIComponent(project.project_id)}?confirm=true`,
          {method: "DELETE"});
        close();
        toast(`项目已删除：${Number(result.detached_sessions || 0)} 个会话、`
          + `${Number(result.detached_observations || 0)} 条观测转为未分类`);
        if ((result.failed_sessions || []).length) {
          toast(`${result.failed_sessions.length} 个会话未能移出，请到会话列表手动移动`, true);
        }
        backToGrid();
        await loadCatalog(true);
        await refreshSessions();
      } catch (e) {
        dlg.querySelector(".pj-error").textContent = e.message;
        button.disabled = false;
      }
    };
    dlg.showModal();
  }

  /* 会话头部/项目页共用的移动入口：保存后刷新列表与项目计数。 */
  function moveSession(sessionId) {
    const session = (S.sessions || []).find(x => x.session_id === sessionId);
    if (!session) return;
    if (!catalog) {
      loadCatalog().then(() => moveSession(sessionId)).catch(e => toast(e.message, true));
      return;
    }
    fillSelect($("#mv-project"), session.project_id || "");
    const dlg = $("#mv-dlg");
    dlg.dataset.sessionId = sessionId;
    dlg.showModal();
  }

  async function loadCatalog(silent = false) {
    const data = await api("/api/projects");
    catalog = data;
    S.projects = data.projects || [];
    fillSelect($("#c-project"), $("#c-project")?.value || "");
    renderSessions();
    if (S.view === "projects") {
      renderGrid();
      if (current !== null) {
        const f = folder(current);
        if (!f) { current = null; renderGrid(); }
        else {
          const stats = $("#project-detail .pd-stats");
          if (stats) stats.innerHTML = statsHtml(f);
          const title = $("#project-detail .pd-title h3");
          if (title) title.textContent = f.name || "未分类";
        }
      }
    }
    return data;
  }

  function open() {
    close();
    current = null; tab = "assets";
    shell();
    renderGrid();
    loadCatalog(true).catch(e => toast(e.message, true));
    poll = setInterval(() => {
      if (S.view !== "projects" || document.visibilityState !== "visible") return;
      loadCatalog(true).catch(() => {});
    }, 6000);
  }

  function close() {
    if (poll) { clearInterval(poll); poll = null; }
  }

  /* 页面级交互（脚本加载时 DOM 已就绪） */
  document.querySelectorAll("[data-project]").forEach(button => button.onclick = () => {
    S.cur = null; show("projects"); renderSessions();
  });
  $("#c-project-new").onclick = () => editDialog(null);
  $("#mv-cancel").onclick = () => $("#mv-dlg").close();
  $("#mv-save").onclick = async () => {
    const dlg = $("#mv-dlg"), sessionId = dlg.dataset.sessionId;
    const projectId = $("#mv-project").value;
    const button = $("#mv-save");
    button.disabled = true;
    try {
      await api(`/api/sessions/${encodeURIComponent(sessionId)}`,
        {method: "PATCH", body: JSON.stringify({project_id: projectId})});
      dlg.close();
      toast("会话已移动；后续观测归入新项目，已注入的项目记忆不会即时重载");
      await refreshSessions();
      if (S.cur === sessionId) refreshState();
      loadCatalog(true).catch(() => {});
    } catch (e) { toast(e.message, true); }
    finally { button.disabled = false; }
  };

  return {open, close, loadCatalog, fillSelect, moveSession, projectCardsHtml};
})();
