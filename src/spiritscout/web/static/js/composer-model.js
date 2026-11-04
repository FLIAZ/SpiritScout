"use strict";
const ComposerModels = (() => {
  const label = entry => entry.provider_ref ? `${entry.provider_ref} / ${entry.model}` : entry.model;
  function options(data) {
    return [{key: "", label: "自动 · 跟随编队"}, ...(data?.models || [])
      .filter(entry => entry.enabled !== false)
      .map(entry => ({key: entry.key || entry.model, label: label(entry)}))];
  }
  function create({request, sessionId, sessionModel, saveSession}) {
    let home = "";
    const pending = new Set();
    return {
      selected: kind => kind === "home" ? home : sessionModel(),
      pending: kind => kind === "home" ? false : pending.has(sessionId()),
      async choose(kind, model) {
        if (kind === "home") { home = model; return; }
        const id = sessionId();
        if (!id || pending.has(id)) return;
        pending.add(id);
        try {
          const updated = await request(`/api/sessions/${encodeURIComponent(id)}/model`,
            {method: "POST", body: JSON.stringify({model})});
          saveSession(id, updated.model || "");
        } finally { pending.delete(id); }
      },
    };
  }
  let controller, opened = null, rows = [], generation = 0;
  function currentModel() {
    if (S.state?.session?.session_id === S.cur) return S.state.session.model || "";
    return S.sessions.find(session => session.session_id === S.cur)?.model || "";
  }
  function init() {
    if (controller) return;
    controller = create({request: api, sessionId: () => S.cur, sessionModel: currentModel,
      saveSession(id, model) {
        const record = S.sessions.find(session => session.session_id === id);
        if (record) record.model = model;
        if (S.state?.session?.session_id === id) S.state.session.model = model;
      }});
    for (const kind of ["home", "comp"]) {
      const button = $(`#${kind}-model`);
      if (button) button.onclick = () => opened === kind ? close() : open(kind);
    }
    document.addEventListener("click", event => {
      if (opened && !event.target.closest(".model-picker")) close();
    });
    document.addEventListener("keydown", event => {
      if (!opened) return;
      if (event.key === "Escape") { event.preventDefault(); close(true); }
      if (["ArrowDown", "ArrowUp"].includes(event.key)) {
        const buttons = [...$(`#${opened}-model-menu`).querySelectorAll('[role="option"]')];
        if (!buttons.length) return;
        const index = buttons.indexOf(document.activeElement);
        event.preventDefault();
        buttons[(index + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length].focus();
      }
    });
    sync();
  }
  function close(focus = false) {
    if (!opened) return;
    const kind = opened;
    opened = null; generation++;
    $(`#${kind}-model-menu`).hidden = true;
    $(`#${kind}-model`).setAttribute("aria-expanded", "false");
    if (focus) $(`#${kind}-model`).focus();
  }
  function positionMenu() {
    if (!opened) return;
    const menu = $(`#${opened}-model-menu`), rect = $(`#${opened}-model`).getBoundingClientRect();
    const width = Math.min(340, window.innerWidth - 32);
    Object.assign(menu.style, {position: "fixed", width: `${width}px`, right: "auto", bottom: "auto"});
    menu.style.left = `${Math.max(16, Math.min(rect.right - width, window.innerWidth - width - 16))}px`;
    menu.style.top = `${Math.max(16, Math.min(rect.top - menu.offsetHeight - 8, window.innerHeight - menu.offsetHeight - 16))}px`;
  }
  function sync() {
    if (!controller) return;
    for (const kind of ["home", "comp"]) {
      const button = $(`#${kind}-model`);
      if (!button) continue;
      const ref = controller.selected(kind), item = rows.find(row => row.key === ref);
      const text = !ref ? "自动" : item?.label || modelDisplayName(ref);
      button.querySelector("span").textContent = text;
      button.title = ref ? `本会话使用 ${text}；子 Agent 显式指派不变` : "自动 · 跟随编队";
      button.disabled = controller.pending(kind) || (kind === "comp" && !S.cur);
    }
  }
  async function open(kind) {
    init(); close(); opened = kind;
    const token = ++generation, menu = $(`#${kind}-model-menu`);
    const id = S.cur;
    menu.hidden = false;
    menu.innerHTML = '<p role="status">正在读取模型…</p>';
    positionMenu();
    $(`#${kind}-model`).setAttribute("aria-expanded", "true");
    try {
      rows = options(await api("/api/models"));
      if (token !== generation || opened !== kind || (kind === "comp" && S.cur !== id)) return;
      menu.innerHTML = '<label class="model-search">选择模型<input type="search" placeholder="搜索模型或供应商…" aria-label="搜索模型或供应商"></label><div role="listbox" aria-label="会话模型"></div>';
      const search = menu.querySelector("input"), list = menu.querySelector('[role="listbox"]');
      function paint() {
        const query = search.value.trim().toLowerCase();
        const shown = rows.filter(row => !row.key || row.label.toLowerCase().includes(query));
        list.innerHTML = shown.map(row => `<button type="button" role="option" aria-selected="${controller.selected(kind) === row.key}" data-model="${esc(row.key)}"><span>${esc(row.label)}</span><b aria-hidden="true">${controller.selected(kind) === row.key ? "✓" : ""}</b></button>`).join("");
        list.querySelectorAll("[data-model]").forEach(button => button.onclick = async () => {
          if (kind === "comp" && S.cur !== id) { close(); return; }
          list.querySelectorAll("button").forEach(item => { item.disabled = true; });
          const operation = controller.choose(kind, button.dataset.model);
          sync();
          try { await operation; close(true); }
          catch (error) { toast(`模型未切换：${error.message}`, true); paint(); }
          finally { sync(); }
        });
      }
      search.oninput = () => { paint(); positionMenu(); };
      paint(); positionMenu(); search.focus(); sync();
    } catch (error) {
      if (token !== generation) return;
      menu.innerHTML = `<p role="alert">读取失败：${esc(error.message)}</p><button type="button">重试</button>`;
      menu.querySelector("button").onclick = () => open(kind);
    }
  }
  return {options, create, init, sync, close,
    selected: kind => { init(); return controller.selected(kind); },
    pending: () => { init(); return controller.pending("comp"); }};
})();
