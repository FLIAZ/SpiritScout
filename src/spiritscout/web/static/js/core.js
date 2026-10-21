"use strict";
"use strict";
/* ================= 基础 ================= */
const $ = s => document.querySelector(s);
const S = { view: "home", cur: null, tab: "chat", sessions: [], state: null,
            agents: null, models: null, evSeen: 0, es: null, poll: null, medit: null,
            providers: null, pedit: null, fprov: "", presets: null, effSel: [],
            topoView: null, compFiles: [], homeFiles: [], think: new Map(),
            projects: [] };
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

async function api(path, opt = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opt });
  if (!r.ok) {
    let detail = null, msg = r.statusText;
    try { detail = (await r.json()).detail; } catch {}
    if (detail && typeof detail === "object") msg = detail.message || msg;
    else if (detail) msg = detail;
    const err = new Error(msg);
    err.detail = detail;               // 结构化错误（如规则冲突清单）随异常上抛
    throw err;
  }
  const result = await r.json();
  return result;
}
function toast(msg, err = false) {
  const el = document.createElement("div");
  el.className = "toast" + (err ? " err" : "");
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 4200);
}
const TERMINAL = new Set(["completed", "failed", "stopped", "interrupted"]);
const STATUS_CN = { created: "排队中", running: "运行中", completed: "已完成",
                    failed: "失败", stopped: "已停止", interrupted: "已中断" };
const MODE_CN = { src: "SRC 渗透", ctf: "CTF 解题", competition: "渗透赛",
                  dialogue: "对话", rules: "规则" };
const TASK_CN = { business_context: "业务体验与上下文", recon: "侦察测绘", port_scan: "端口扫描", web_probe: "Web 探测",
                  host_probe: "主机探测", post_exploit: "后渗透", report: "报告生成",
                  ctf_web: "Web 题", ctf_pwn: "Pwn 题", ctf_reverse: "逆向题",
                  ctf_crypto: "密码题", ctf_misc: "杂项题" };
const SEV_CN = { critical: ["严重", "red"], high: ["高危", "red"], medium: ["中危", "orange"],
                 low: ["低危", "blue"], info: ["信息", "blue"] };
function agentName(key) {
  if (!S.agents) return key || "—";
  const a = S.agents.roster.find(x => x.key === key);
  return a ? a.name : (key || "—");
}
/* 审计 actor 形如 ctf-web-094cd1652967（键+实例短码）：还原为编队显示名 */
function actorName(actor) {
  const base = String(actor || "").replace(/-[0-9a-f]{8,}$/i, "").replace(/-/g, "_");
  if (!S.agents) return actor || "—";
  const a = S.agents.roster.find(x => x.key === base || x.key === actor);
  return a ? a.name : (actor || "—");
}
/* 项目名缓存由项目页加载；取不到（未分类/项目已删）按未分类显示 */
function projectName(id) {
  if (!id) return "未分类";
  const hit = (S.projects || []).find(x => x.project_id === id);
  return hit ? hit.name : "未分类";
}
function fmtElapsed(sec) {
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), ss = sec % 60;
  const pad = n => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(ss)}` : `${m}:${pad(ss)}`;
}
/* 事件时刻 = 任务累积时间（相对会话开始的 0:00 计时，免疫时区） */
function evElapsed(ts) {
  const t0 = S.state && S.state.session && S.state.session.started_at;
  if (!t0 || !ts) return "";
  const t = new Date(ts).getTime();
  return isFinite(t) ? fmtElapsed(t / 1000 - t0) : "";
}
function fmtDur(a, b) {
  if (!a) return "—";
  const s = Math.max(0, Math.round(((b || Date.now() / 1000) - a)));
  const m = Math.floor(s / 60);
  return m >= 60 ? `${Math.floor(m / 60)}h${m % 60}m` : m ? `${m}m${s % 60}s` : `${s}s`;
}

/* ================= 主题 ================= */
function applyTheme(t) {
  const root = document.documentElement;
  if (t === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", t);
  try { localStorage.setItem("ss-theme", t); } catch {}
}
$("#btn-theme").onclick = () => {
  const cur = (() => { try { return localStorage.getItem("ss-theme") || "system"; } catch { return "system"; } })();
  const next = { system: "light", light: "dark", dark: "system" }[cur];
  applyTheme(next);
  toast({ system: "跟随系统", light: "浅色", dark: "深色" }[next] + "主题");
};
try { applyTheme(localStorage.getItem("ss-theme") || "system"); } catch {}

/* ================= 视图切换 ================= */
const VIEWS = { home: "#view-home", session: "#view-session", dash: "#view-dash",
                agents: "#view-agents", models: "#view-models", mcp:"#view-mcp", skill:"#view-skill", projects:"#view-projects", exp:"#view-exp" };
function show(view) {
  S.view = view;
  for (const [k, sel] of Object.entries(VIEWS)) $(sel).hidden = k !== view;
  document.querySelectorAll(".side > nav button").forEach(b =>
    b.classList.toggle("on", b.dataset.nav === (view === "session" ? "home" : view)));
  if (view === "agents") loadAgents();
  if (view === "models") loadModels();
  if (view === "dash") renderDash();
  if (view === "projects" && typeof ProjectsPage !== "undefined") ProjectsPage.open();
  else if (typeof ProjectsPage !== "undefined") ProjectsPage.close();
  if (["mcp","skill"].includes(view) && typeof ResourceConsole !== "undefined") ResourceConsole.open(view);
  if (view === "exp" && typeof ExperiencePage !== "undefined") ExperiencePage.open();
  if (view !== "session") stopStreams();
}
document.querySelectorAll(".side > nav button").forEach(b =>
  b.onclick = () => { S.cur = null; show(b.dataset.nav); renderSessions(); });
$("#btn-new").onclick = () => { S.cur = null; show("home"); renderSessions(); };

/* ================= 会话列表 ================= */
let sessionsInFlight = null;
function refreshSessions() {
  if (sessionsInFlight) return sessionsInFlight;
  sessionsInFlight = fetchSessions().finally(() => { sessionsInFlight = null; });
  return sessionsInFlight;
}
async function fetchSessions() {
  S.polling?.listRequested();
  try {
    const d = await api("/api/sessions");
    S.sessions = d.sessions;
    $("#connection-status")?.remove();
    renderSessions();
    if (S.view === "dash") renderDash();
    if (S.cur) {
      const cur = S.sessions.find(x => x.session_id === S.cur);
      if (cur && S.state && S.state.session.status !== cur.status) await refreshState();
      if (cur && S.view === "session" && !TERMINAL.has(cur.status) && !S.es) startStreams(false);
    }
  } catch (e) {
    if (!$("#connection-status")) {
      const notice = document.createElement("div"); notice.id = "connection-status"; notice.setAttribute("role", "status");
      notice.textContent = "控制台连接中断，正在重试；当前内容可能不是最新。"; document.body.append(notice);
    }
  }
}
function renderSessions() {
  const box = $("#sess-list");
  if (box.querySelector("input.rename, [data-armed]")) return;   // 正在重命名：不打断输入
  if (!S.sessions.length) {
    box.innerHTML = `<div class="empty-note" style="padding:22px 8px">还没有会话</div>`;
    return;
  }
  box.innerHTML = S.sessions.map(s => `
    <div class="sess-item ${s.session_id === S.cur ? "on" : ""}" data-id="${s.session_id}">
      <div class="t"><span class="pill ${s.status}" style="padding:1px 7px;font-size:10.5px">${STATUS_CN[s.status] || s.status}</span>
        <span class="name" title="${esc(s.title)}">${esc(s.title)}</span></div>
      <div class="s">${esc(s.target || s.session_id)} · <span class="mono">${esc(s.session_id.slice(5, 13))}</span>${s.project_id ? ` · <span class="sess-proj">${esc(projectName(s.project_id))}</span>` : ""}</div>
      <div class="ops">
        <button data-act="rename" title="重命名">✎</button>
        <button data-act="del" class="danger" title="删除会话">✕</button>
      </div>
    </div>`).join("");
  box.querySelectorAll(".sess-item").forEach(el => {
    el.onclick = (ev) => {
      if (!ev.target.closest(".ops") && !ev.target.closest("input"))
        openSession(el.dataset.id);
    };
    el.querySelector('[data-act="rename"]').onclick = (ev) => {
      ev.stopPropagation(); startRename(el.dataset.id, el.querySelector(".name"));
    };
    el.querySelector('[data-act="del"]').onclick = (ev) => {
      ev.stopPropagation(); armDelete(ev.currentTarget, el.dataset.id);
    };
  });
}

/* 重命名：就地编辑，回车提交 / Esc 取消；清空提交 = 恢复自动命名。
   输入框以兄弟节点浮出、原名隐藏，避免破坏被隐藏节点的既有渲染逻辑。 */
function startRename(id, host) {
  if (!host || host.parentElement.querySelector("input.rename")) return;
  const cur = S.sessions.find(s => s.session_id === id) || {};
  const input = document.createElement("input");
  input.className = "rename";
  input.value = cur.title || "";
  input.placeholder = cur.title || "会话名";
  input.title = "回车保存；清空并回车恢复自动命名";
  host.style.display = "none";
  host.after(input);
  input.focus(); input.select();
  let settled = false;
  const restore = () => { input.remove(); host.style.display = ""; };
  const commit = async () => {
    if (settled) return; settled = true;
    const next = input.value.trim();
    restore();
    try {
      await api(`/api/sessions/${id}`, {
        method: "PATCH", body: JSON.stringify({ title: next }) });
      toast(next ? "已重命名" : "已恢复自动命名");
    } catch (e) { toast(e.message, true); }
    await refreshSessions();
    if (S.cur === id) refreshState();
  };
  input.onkeydown = (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); commit(); }
    else if (ev.key === "Escape") {
      settled = true; restore(); renderSessions();
      if (S.cur === id) refreshState();
    }
  };
  input.onblur = commit;
  input.onclick = (ev) => ev.stopPropagation();
}

/* 两段式确认：首击变「删除?」，3 秒内再击才执行 run（会话/经验等删除共用） */
function armConfirm(btn, run) {
  if (btn.dataset.armed) {
    delete btn.dataset.armed;
    btn.disabled = true;
    Promise.resolve().then(run).catch(() => {}).finally(() => {
      btn.disabled = false;
      btn.classList.remove("armed");
      btn.textContent = btn.dataset.label || "删除";
    });
    return;
  }
  btn.dataset.armed = "1"; btn.classList.add("armed");
  btn.dataset.label = btn.textContent; btn.textContent = "删除?";
  setTimeout(() => {
    if (btn.dataset.armed) {
      delete btn.dataset.armed; btn.classList.remove("armed"); btn.textContent = btn.dataset.label;
    }
  }, 3000);
}
function armDelete(btn, id) { armConfirm(btn, () => doDelete(id)); }
async function doDelete(id) {
  try {
    await api(`/api/sessions/${id}?confirm=true`, { method: "DELETE" });
    toast("会话已删除");
    if (S.cur === id) { S.cur = null; show("home"); }
    refreshSessions();
  } catch (e) { toast(e.message, true); }
}


/* ================= 自定义下拉：全站 <select> 增强 ================= */
/* 原生 select 留在 DOM 管值与 change 事件（既有监听不动，元素身份不变），
   视觉与交互由触发框 + 浮层面板接管：键盘可达（↑↓ Enter Esc Home End）、
   点外即关、选中打勾、贴视口下缘自动上翻。不依赖试验性的
   appearance:base-select（Firefox/Safari 尚未默认支持，跨浏览器一致）。 */
function selxSync(sel) {
  const wrap = sel.closest(".selx");
  if (!wrap) return;
  const o = sel.selectedOptions[0];
  const prov = o?.dataset.prov || "";
  wrap.querySelector(".selx-label").textContent =
    prov ? `${prov} ｜ ${o.textContent}` : (o?.textContent ?? "");
  wrap.querySelector(".selx-btn").disabled = sel.disabled;
}
function selxRebuild(sel) {
  const list = sel.closest(".selx").querySelector(".selx-list");
  list.innerHTML = "";
  for (const o of sel.options) {
    if (o.hidden) continue;
    const row = document.createElement("div");
    row.className = "selx-opt" + (o.selected ? " on" : "") + (o.disabled ? " dis" : "");
    row.dataset.value = o.value;
    const prov = o.dataset.prov || "";
    row.innerHTML = (prov ? `<span class="selx-prov">${esc(prov)}</span>` : "") +
      `<span class="selx-txt">${esc(o.textContent)}</span>` +
      (o.selected ? `<i class="selx-check"></i>` : "");
    list.append(row);
  }
}
function enhanceSelect(sel) {
  if (sel.dataset.selx || sel.multiple) return;
  sel.dataset.selx = "1";
  const wrap = document.createElement("div");
  wrap.className = "selx";
  sel.replaceWith(wrap);
  wrap.append(sel);
  sel.classList.add("selx-native");

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "selx-btn";
  btn.innerHTML = `<span class="selx-label"></span><i class="selx-chev"></i>`;
  const panel = document.createElement("div");
  panel.className = "selx-panel";
  const list = document.createElement("div");
  list.className = "selx-list";
  panel.append(list);
  wrap.append(btn, panel);

  const paint = () => [...list.children].forEach((r, i) =>
    r.classList.toggle("cur", i === cur));
  const choose = (row) => {
    if (!row || row.classList.contains("dis")) return;
    if (sel.value !== row.dataset.value) {
      sel.value = row.dataset.value;
      sel.dispatchEvent(new Event("change", { bubbles: true }));
    }
    selxSync(sel);
    close();
    btn.focus();
  };
  const close = () => wrap.classList.remove("open");
  let cur = -1;
  const open = () => {
    selxRebuild(sel);
    selxSync(sel);
    wrap.classList.add("open");
    const on = list.querySelector(".on");
    cur = Math.max(0, [...list.children].indexOf(on));
    paint();
    // 贴视口下缘翻到上方展开
    const r = btn.getBoundingClientRect();
    wrap.classList.toggle("up",
      r.bottom + Math.min(panel.scrollHeight, 300) + 14 > innerHeight &&
      r.top - Math.min(panel.scrollHeight, 300) - 14 > 0);
    panel.scrollTop = on ? Math.max(0, on.offsetTop - 120) : 0;
  };

  btn.addEventListener("click", () => wrap.classList.contains("open") ? close() : open());
  btn.addEventListener("keydown", (e) => {
    const keys = ["ArrowDown", "ArrowUp", "Enter", " "];
    if (!wrap.classList.contains("open") && keys.includes(e.key)) {
      e.preventDefault(); open(); return;
    }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      cur = Math.min(Math.max(cur + (e.key === "ArrowDown" ? 1 : -1), 0),
                     list.children.length - 1);
      paint();
      list.children[cur]?.scrollIntoView({ block: "nearest" });
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault(); choose(list.children[cur]);
    } else if (e.key === "Escape") { close(); }
    else if (e.key === "Home") { e.preventDefault(); cur = 0; paint(); }
    else if (e.key === "End") {
      e.preventDefault(); cur = list.children.length - 1; paint();
    }
  });
  list.addEventListener("click", (e) => {
    const row = e.target.closest(".selx-opt");
    if (row) choose(row);
  });
  selxSync(sel);
}
function enhanceAllSelects(root = document) {
  root.querySelectorAll("select").forEach(enhanceSelect);
}
/* 动态渲染的 select（编队卡/弹窗模板）落进 DOM 即增强 */
new MutationObserver((muts) => {
  for (const m of muts) for (const n of m.addedNodes) {
    if (n.nodeType !== 1) continue;
    if (n.tagName === "SELECT") enhanceSelect(n);
    else if (n.querySelector) n.querySelectorAll("select").forEach(enhanceSelect);
  }
}).observe(document.body, { childList: true, subtree: true });
/* 点面板外即收起（pointerdown 捕获触发框内外的边界情形） */
document.addEventListener("pointerdown", (e) => {
  document.querySelectorAll(".selx.open").forEach(w => {
    if (!w.contains(e.target)) w.classList.remove("open");
  });
});
enhanceAllSelects();
