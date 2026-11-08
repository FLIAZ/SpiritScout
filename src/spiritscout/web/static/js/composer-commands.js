"use strict";
const ComposerCommands = (() => {
  const commands = [
    {name:"compact", description:"手动压缩执行上下文", detail:"下次调用前整理旧历史，保留原始记录"},
    {name:"status", description:"查看任务与压缩状态", detail:"读取当前状态，不调用模型"},
    {name:"pause", description:"暂停当前任务", detail:"当前调用返回后暂停，保留进度"},
    {name:"resume", description:"继续已暂停的任务", detail:"从原进度继续执行"},
    {name:"model", description:"选择会话模型", detail:"打开模型选择器"},
    {name:"files", description:"查看任务文件", detail:"打开输出产物"},
    {name:"report", description:"查看任务报告", detail:"打开已有报告"},
    {name:"help", description:"查看命令帮助", detail:"列出可用命令与快捷键"},
  ];
  const bindings = new Map(), retries = new Map();
  let opened = null, selected = 0, shown = [], generation = 0;
  const tr = text => I18n.t(text);
  function parse(text) {
    const value = text.trim();
    if (value === "/") return {name:"", args:""};
    const match = /^\/([a-z][a-z0-9_-]*)(?:[ \t]+([^\r\n]*))?$/i.exec(value);
    return match ? {name:match[1].toLowerCase(), args:match[2] || ""} : null;
  }
  const messageText = text => text.startsWith("//") ? text.slice(1) : text;
  const matches = query => commands.filter(item => item.name.includes(query.toLowerCase()) ||
    tr(item.description).toLowerCase().includes(query.toLowerCase()));
  function session() {
    return S.state?.session?.session_id === S.cur ? S.state.session : S.sessions.find(s => s.session_id === S.cur);
  }
  function unavailable(command, kind) {
    if (["help", "model"].includes(command.name) && kind === "home") return "";
    if (kind === "home" || !S.cur) return tr("先打开任务会话");
    const s = session();
    if (command.name === "compact" && s?.side_session) return tr("请在主任务会话中压缩执行上下文");
    if (command.name === "pause" && (s?.paused || !s || TERMINAL.has(s.status))) return tr("当前没有可暂停的任务");
    if (command.name === "resume" && !s?.paused) return tr("当前任务未暂停");
    return "";
  }
  function close() {
    if (!opened) return;
    const binding = bindings.get(opened);
    binding.menu.hidden = true;
    binding.input.setAttribute("aria-expanded", "false");
    binding.input.removeAttribute("aria-activedescendant");
    opened = null;
  }
  function position() {
    if (!opened) return;
    const {input, menu} = bindings.get(opened), rect = input.getBoundingClientRect();
    const width = Math.min(rect.width, window.innerWidth - 24);
    menu.style.width = `${width}px`;
    menu.style.left = `${Math.max(12, Math.min(rect.left, window.innerWidth - width - 12))}px`;
    const height = menu.offsetHeight;
    const top = rect.top >= height + 20 ? rect.top - height - 8 : rect.bottom + 8;
    menu.style.top = `${Math.max(12, Math.min(top, window.innerHeight - height - 12))}px`;
  }
  function highlight() {
    if (!opened) return;
    const {input, menu} = bindings.get(opened);
    menu.querySelectorAll("[data-command]").forEach((button, index) => {
      button.setAttribute("aria-selected", String(index === selected));
      if (index === selected) {
        input.setAttribute("aria-activedescendant", button.id);
        button.scrollIntoView({block:"nearest"});
      }
    });
    if (!shown.length) input.removeAttribute("aria-activedescendant");
  }
  function show(kind) {
    const binding = bindings.get(kind);
    if (!binding || binding.input.disabled) return close();
    const match = /^\/([^\s/]*)$/.exec(binding.input.value.trim());
    if (!match) return close();
    close(); opened = kind; selected = 0;
    shown = matches(match[1]);
    const {menu, input} = binding;
    menu.innerHTML = I18n.html`<div class="command-menu-head"><span>Agent 命令</span><small>↑↓ 选择 · Tab 补全 · Esc 关闭</small></div>` +
      `<div role="listbox" id="${kind}-command-list" aria-label="${esc(tr("Agent 命令"))}">${shown.map((item, index) => {
        const reason = unavailable(item, kind);
        return `<button type="button" role="option" tabindex="-1" id="${kind}-command-${index}" data-command="${item.name}" aria-disabled="${!!reason}" aria-selected="false"><code>/${item.name}</code><span>${esc(tr(item.description))}<small>${esc(reason || tr(item.detail))}</small></span></button>`;
      }).join("")}</div>` + (shown.length ? "" : I18n.html`<p class="command-empty">没有匹配的命令。输入 /help 查看帮助；以 // 开头可发送原文。</p>`);
    menu.querySelectorAll("[data-command]").forEach(button => {
      button.onmousedown = event => event.preventDefault();
      button.onclick = () => choose(button.dataset.command);
    });
    menu.hidden = false;
    input.setAttribute("aria-expanded", "true");
    position(); highlight();
  }
  function choose(name) {
    if (!opened) return;
    const kind = opened, command = commands.find(item => item.name === name);
    const reason = unavailable(command, kind);
    if (reason) { toast(reason, true); return; }
    const {input} = bindings.get(kind);
    input.value = `/${name}`; close(); input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
    if (kind === "session") compGrow(); else homeSync();
  }
  function keydown(event, kind) {
    if (event.isComposing || event.keyCode === 229 || opened !== kind) return false;
    if (event.key === "Escape") { event.preventDefault(); close(); return true; }
    if (["ArrowDown", "ArrowUp"].includes(event.key) && shown.length) {
      event.preventDefault();
      selected = (selected + (event.key === "ArrowDown" ? 1 : -1) + shown.length) % shown.length;
      highlight(); return true;
    }
    if ((event.key === "Tab" && !event.shiftKey) || (event.key === "Enter" && !event.shiftKey)) {
      const parsed = parse(bindings.get(kind).input.value);
      if (event.key === "Enter" && commands.some(item => item.name === parsed?.name)) { close(); return false; }
      if (shown.length) { event.preventDefault(); choose(shown[selected].name); return true; }
    }
    return false;
  }
  function feedback(kind, title, content) {
    const panel = bindings.get(kind).feedback;
    panel.innerHTML = `<div class="command-feedback-head"><strong>${esc(title)}</strong><button type="button" aria-label="${esc(tr("关闭"))}">×</button></div>${content}`;
    panel.hidden = false;
    panel.querySelector("button").onclick = () => { panel.hidden = true; };
  }
  function help(kind) {
    feedback(kind, tr("Agent 命令"), `<dl class="command-help">${commands.map(item =>
      `<div><dt><code>/${item.name}</code></dt><dd>${esc(tr(item.description))}</dd></div>`).join("")}</dl>` +
      I18n.html`<p>输入 / 筛选命令，↑↓ 选择，Tab 或 Enter 补全，再按 Enter 执行。Shift+Enter 换行，Esc 关闭。</p><p>命令不接受附件或额外参数。以 // 开头可发送以 / 开头的原文；文件路径与多行文本按普通消息发送。</p>`);
  }
  async function execute(text, files, kind = "session") {
    const parsed = parse(text);
    if (!parsed) return false;
    if (!parsed.name) { show(kind); return true; }
    const command = commands.find(item => item.name === parsed.name);
    if (!command) { toast(tr("未知命令。输入 /help 查看帮助；以 // 开头可发送原文。"), true); return true; }
    if (files.length || parsed.args) { toast(tr("命令不接受附件或额外参数，请先移除附件或单独发送。"), true); return true; }
    const reason = unavailable(command, kind);
    if (reason) { toast(reason, true); return true; }
    const id = S.cur, binding = bindings.get(kind), draft = binding.input.value, token = generation;
    const current = () => token === generation && (kind === "home" ? S.view === "home" : S.cur === id);
    if (kind === "session" && (sessionControlPending.has(id) || sessionMessagePending.has(id))) return true;
    const clear = () => {
      if (!current() || binding.input.value !== draft) return;
      binding.input.value = "";
      if (kind === "session") compGrow(); else homeSync();
    };
    close();
    if (kind === "session") { sessionMessagePending.add(id); syncComposerControl(); }
    try {
      if (command.name === "help") help(kind);
      else if (command.name === "model") $(`#${kind === "home" ? "home" : "comp"}-model`).click();
      else if (command.name === "files" || command.name === "report") {
        $(`#s-tabs [data-tab="${command.name === "files" ? "deliverables" : "report"}"]`).click();
      } else if (command.name === "compact") {
        let requestId = retries.get(id);
        if (!requestId) {
          requestId = globalThis.crypto?.randomUUID?.() || `compact-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
          retries.set(id, requestId);
        }
        await api(`/api/sessions/${encodeURIComponent(id)}/compact`, {method:"POST", body:JSON.stringify({request_id:requestId})});
        retries.delete(id);
        if (current()) feedback(kind, "/compact", I18n.html`<p>已安排手动压缩。各执行上下文将在下一次模型调用前整理一次；暂停或已结束的任务会等待继续。用 /status 查看处理结果。</p>`);
      } else if (command.name === "status") {
        const data = await api(`/api/sessions/${encodeURIComponent(id)}/context`);
        if (current()) {
          const s = data.session, results = data.results || [], last = [...results].sort((a,b) => a.completed_at.localeCompare(b.completed_at)).at(-1);
          let content = `<p>${esc(s.paused ? tr("已暂停") : STATUS_CN[s.status] || s.status)} · ${esc(MODE_CN[s.mode] || s.mode)} · ${esc(s.title || s.target || id)}</p>`;
          if (!data.request) content += I18n.html`<p>本会话尚未请求手动压缩。</p>`;
          else if (!last) content += I18n.html`<p>压缩请求已保存，等待下一次模型调用。</p>`;
          else content += I18n.html`<p>已处理 ${results.length} 个上下文，其中 ${results.filter(row => row.changed).length} 个缩小。最近一次约 ${esc(last.estimated_tokens_before)} → ${esc(last.estimated_tokens_after)} tokens（离线估算）。</p>`;
          feedback(kind, "/status", content);
        }
      } else {
        const updated = await api(`/api/sessions/${encodeURIComponent(id)}/${command.name}`, {method:"POST", body:"{}"});
        const record = S.sessions.find(s => s.session_id === id);
        if (record) Object.assign(record, updated);
        if (current()) {
          if (S.state?.session?.session_id === id) Object.assign(S.state.session, updated);
          feedback(kind, `/${command.name}`, `<p>${esc(command.name === "pause" ? tr("已请求暂停，当前调用返回后暂停") : tr("已继续原任务"))}</p>`);
          refreshState(); refreshSessions();
        }
      }
      clear();
    } catch (error) { if (current()) toast(error.message, true); }
    finally { if (kind === "session") { sessionMessagePending.delete(id); syncComposerControl(); } }
    return true;
  }
  function reset() {
    generation++;
    close();
    for (const binding of bindings.values()) binding.feedback.hidden = true;
  }
  function init() {
    for (const [kind, selector] of [["session", "#comp-input"], ["home", "#c-mission"]]) {
      const input = $(selector), menu = document.createElement("div"), panel = document.createElement("div");
      menu.className = "command-menu"; menu.hidden = true;
      menu.innerHTML = `<div role="listbox" id="${kind}-command-list" aria-label="${esc(tr("Agent 命令"))}"></div>`;
      panel.className = "command-feedback"; panel.hidden = true; panel.setAttribute("role", "status");
      panel.setAttribute("aria-live", "polite");
      input.before(menu, panel);
      input.setAttribute("role", "combobox"); input.setAttribute("aria-autocomplete", "list");
      input.setAttribute("aria-haspopup", "listbox"); input.setAttribute("aria-expanded", "false");
      input.setAttribute("aria-controls", `${kind}-command-list`);
      bindings.set(kind, {input, menu, feedback:panel});
      input.addEventListener("input", event => { if (!event.isComposing) show(kind); });
      input.addEventListener("compositionstart", close);
      input.addEventListener("compositionend", () => show(kind));
      input.addEventListener("blur", () => { if (opened === kind) close(); });
    }
    window.addEventListener("resize", position);
    document.addEventListener("scroll", position, true);
  }
  return {parse, messageText, matches, execute, keydown, reset, init};
})();
ComposerCommands.init();
