"use strict";
"use strict";
/* ================= 基础 ================= */
const $ = s => document.querySelector(s);
const S = { view: "home", cur: null, tab: "chat", sessions: [], state: null,
            agents: null, models: null, evSeen: 0, es: null, poll: null, medit: null,
            providers: null, pedit: null, fprov: "", presets: null, effSel: [],
            topoView: null, compFiles: [], homeFiles: [], think: new Map() };
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
  return r.json();
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
const MODE_CN = { src: "SRC 渗透", ctf: "CTF 解题", competition: "渗透赛" };
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
                agents: "#view-agents", models: "#view-models" };
function show(view) {
  S.view = view;
  for (const [k, sel] of Object.entries(VIEWS)) $(sel).hidden = k !== view;
  document.querySelectorAll("nav button").forEach(b =>
    b.classList.toggle("on", b.dataset.nav === (view === "session" ? "home" : view)));
  if (view === "agents") loadAgents();
  if (view === "models") loadModels();
  if (view === "dash") renderDash();
  if (view !== "session") stopStreams();
}
document.querySelectorAll("nav button").forEach(b =>
  b.onclick = () => { S.cur = null; show(b.dataset.nav); renderSessions(); });
$("#btn-new").onclick = () => { S.cur = null; show("home"); renderSessions(); };

/* ================= 会话列表 ================= */
async function refreshSessions() {
  try {
    const d = await api("/api/sessions");
    S.sessions = d.sessions;
    renderSessions();
    if (S.view === "dash") renderDash();
    if (S.cur) {
      const cur = S.sessions.find(x => x.session_id === S.cur);
      if (cur && TERMINAL.has(cur.status) && S.state &&
          S.state.session.status !== cur.status) await refreshState();
    }
  } catch (e) { /* 控制台重启中等场景静默重试 */ }
}
function renderSessions() {
  const box = $("#sess-list");
  if (box.querySelector("input.rename")) return;   // 正在重命名：不打断输入
  if (!S.sessions.length) {
    box.innerHTML = `<div class="empty-note" style="padding:22px 8px">还没有会话</div>`;
    return;
  }
  box.innerHTML = S.sessions.map(s => `
    <div class="sess-item ${s.session_id === S.cur ? "on" : ""}" data-id="${s.session_id}">
      <div class="t"><span class="pill ${s.status}" style="padding:1px 7px;font-size:10.5px">${STATUS_CN[s.status] || s.status}</span>
        <span class="name" title="${esc(s.title)}">${esc(s.title)}</span></div>
      <div class="s">${esc(s.target || s.session_id)} · <span class="mono">${esc(s.session_id.slice(5, 13))}</span></div>
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

/* 删除：两段式确认（首击变「删除?」，3 秒内再击生效） */
function armDelete(btn, id) {
  if (btn.dataset.armed) { delete btn.dataset.armed; doDelete(id); return; }
  btn.dataset.armed = "1"; btn.classList.add("armed");
  const old = btn.textContent; btn.textContent = "删除?";
  setTimeout(() => {
    if (btn.dataset.armed) {
      delete btn.dataset.armed; btn.classList.remove("armed"); btn.textContent = old;
    }
  }, 3000);
}
async function doDelete(id) {
  try {
    await api(`/api/sessions/${id}?confirm=true`, { method: "DELETE" });
    toast("会话已删除");
    if (S.cur === id) { S.cur = null; show("home"); }
    refreshSessions();
  } catch (e) { toast(e.message, true); }
}

