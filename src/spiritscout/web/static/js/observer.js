"use strict";
/* 会话实况：只展示真实事件与已持久化证据，不推测模型的内部思考。 */
const SessionObserver = (() => {
  let events = [], scheduled = false;
  const number = n => Number.isFinite(Number(n)) ? Number(n).toLocaleString("zh-CN") : "—";
  const names = { explore: "业务走访", record_endpoint: "登记接口", http_probe: "HTTP 请求",
    shell: "执行命令", python: "运行脚本", browser_navigate: "浏览器打开页面",
    browser_click: "点击页面元素", browser_snapshot: "读取页面快照", browser_tabs: "管理浏览器标签页",
    meta_planning: "规划与复盘", verification: "独立危害复测", execution: "任务执行" };
  const toolName = name => {
    const short = String(name || "工具").startsWith("mcp__") ? String(name).split("__").slice(2).join("__") : name;
    return names[name] || names[short] || short;
  };
  const age = ts => {
    const seconds = Math.max(0, Math.floor((Date.now() - Date.parse(ts)) / 1000));
    return seconds < 60 ? `${seconds} 秒前` : seconds < 3600 ? `${Math.floor(seconds / 60)} 分钟前` : `${Math.floor(seconds / 3600)} 小时前`;
  };
  function usageFor(event, state) {
    const d = event.detail || {};
    if (typeof d.tokens === "number") return d.tokens;
    // 旧日志误脱敏了数字。只匹配同模型、同开始时刻的真实计量记录，
    // 不按平均数分摊、不把总量当作单次消耗。
    const start = Date.parse(event.ts) / 1000 - Number(d.latency_ms || 0) / 1000;
    const candidates = (state?.model_usage?.call_series || [])
      .filter(x => x.model === d.model && Math.abs(x.ts - start) < 1)
      .sort((a, b) => Math.abs(a.ts - start) - Math.abs(b.ts - start));
    return candidates[0]?.tokens ?? null;
  }
  const cleanText = value => String(value || "").replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "");
  function resultSummary(detail) {
    if (detail.ok === false && detail.failure_summary) return String(detail.failure_summary);
    const text = cleanText(detail.note || detail.result || detail.error).trim();
    if (detail.ok === false && /element is outside of the viewport/i.test(text))
      return "目标元素已找到，但位于可视区域外；自动滚动后仍无法点击。";
    const title = text.match(/<title[^>]*>([^<]+)/i);
    if (title) return `返回网页 · ${title[1]}`;
    if (/^\s*<!doctype|^\s*<html/i.test(text)) return "返回 HTML 页面，展开查看响应摘要";
    const code = text.match(/"code"\s*:\s*(-?\d+)/);
    const message = text.match(/"message"\s*:\s*"([^"\n]*)"/);
    if (code && code[1] !== "0") return `业务返回 ${code[1]}${message ? ` · ${message[1]}` : ""}`;
    return text.slice(0, 220) || (detail.ok ? "调用已返回，未提供结果摘要" : "调用失败，未提供原因");
  }
  function derive(state, history = events) {
    const usage = state?.model_usage || {}, business = state?.business_context || {};
    const calls = history.filter(e => e.type === "model.call");
    const series = usage.call_series || [];
    const task = (state?.tasks || []).find(t => t.status === "running");
    const recent = history.filter(e => e.type === "agent.tool_result" && !String(e.detail?.id).includes(":experience_normal_flow:")).slice(-4).reverse();
    const pending = new Map();
    for (const e of history) {
      if (e.type === "model.started") pending.set(e.detail?.call_id, e);
      if (["model.call", "model.failed", "model.cancelled"].includes(e.type)) pending.delete(e.detail?.call_id);
    }
    return { usage, business, calls, task, recent, pending: [...pending.values()],
      tokens: usage.used_tokens ?? state?.session?.tokens ?? 0,
      callCount: usage.calls ?? series.length,
      recentTokens: calls.filter(e => Date.parse(e.ts) >= Date.now() - 60000)
        .reduce((sum, e) => sum + Number(usageFor(e, state) || 0), 0),
      evidence: Array.isArray(business.evidence) && business.evidence.length ? business.evidence : (state?.observed_evidence || []),
      cache: usage.cache || {},
      failed: history.filter(e => e.type === "agent.tool_result" && e.detail?.ok === false).length };
  }
  function render() {
    scheduled = false;
    if (S.view !== "session" || S.tab !== "chat") return;
    const box = $("#session-observer"), state = S.state;
    if (!box || !state || state.session?.session_id !== S.cur) return;
    const v = derive(state), paused = state.session.paused, terminal = TERMINAL.has(state.session.status);
    const tasks = state.tasks || [], running = tasks.filter(t => t.status === "running");
    const latest = events[events.length - 1];
    const active = v.task ? (TASK_CN[v.task.task_type] || agentName(v.task.agent_type)) : "灵枢";
    const current = paused ? "已暂停" : terminal ? (STATUS_CN[state.session.status] || "已结束")
      : v.pending.length ? "模型正在生成" : "任务执行中";
    const routes = (v.task?.routes || []).filter(r => r.status === "running");
    const note = routes.map(r => r.note).filter(Boolean).join("；");
    const signature = JSON.stringify([current, active, note, v.tokens, v.callCount, v.recentTokens,
      v.cache, v.evidence, state.layers?.asset, v.business.facts, (v.business.gaps || v.business.unknowns), v.failed, v.recent, tasks, v.pending, latest?.ts,
      Math.floor(Date.now() / 60000)]);
    if (box.dataset.signature === signature) return;
    const opened = new Set([...box.querySelectorAll("details[open]")].map(x => x.dataset.section));
    box.dataset.signature = signature;
    box.innerHTML = `<div class="obs-focus"><span class="obs-label">当前实况</span>
      <strong>${esc(current)}</strong><span>${esc(active)}</span>
      <p>${esc(paused ? "暂停会在步骤边界生效；在途调用可能仍在收尾。" : terminal ? "本次执行已结束；可查看产出、证据和参谋决策记录。" : note || "等待新的执行事件；未收到事件不代表已停止。")}</p>
      ${paused && note ? `<p class="obs-last">最近进展 · ${esc(note)}</p>` : ""}
      <small>${latest ? `最近事件 ${esc(age(latest.ts))}` : "正在读取已有执行记录…"}</small></div>
      <section class="obs-section"><h3>模型消耗 <span>${number(v.callCount)} 次运行计量</span></h3>
        <div class="obs-tokens">${number(v.tokens)} <small>token</small></div>
        <p>近 1 分钟 +${number(v.recentTokens)} · 已返回调用的计量，生成中尚未结算</p>
        <div class="obs-cache"><span>输入缓存命中率</span><strong>${v.cache.hit_rate == null ? "端点未提供" : (v.cache.hit_rate * 100).toFixed(1) + "%"}</strong>
        <small>${v.cache.measured_calls ? number(v.cache.cached_input_tokens) : "—"} 缓存读取 · ${number(v.cache.measured_calls || 0)} 次已测量；缺失用量不计为零命中</small></div>
        ${v.cache.total_input_tokens != null ? `<p>输入 ${number(v.cache.total_input_tokens)} · 已测未命中 ${number(v.cache.uncached_input_tokens)} · 输出 ${number(v.cache.output_tokens)} token</p>` : ""}
        <details data-section="cache"><summary>按角色查看缓存与输出消耗</summary>${(v.cache.groups || []).map(g => `<div class="obs-call"><span>${esc(actorName(g.actor))} · ${esc(names[g.task_type] || g.task_type)} · ${esc(g.model)}</span><b>${g.hit_rate == null ? "缓存未提供" : `${(g.hit_rate * 100).toFixed(1)}% 输入命中`}</b><small>输入 ${number(g.total_input_tokens)} · 缓存 ${number(g.cached_input_tokens)} · 输出 ${number(g.output_tokens)} · ${number(g.measured_calls)} 次已测量${g.unmeasured_calls ? ` / ${number(g.unmeasured_calls)} 次未提供` : ""}</small></div>`).join("") || '<p>历史调用未保留分角色用量</p>'}<p>缓存命中率只计算端点确认的输入；输出和新增事实仍需要计算。</p></details>
        <div class="obs-models">${Object.entries(v.usage.per_model_tokens || {}).map(([m,n]) => `<div><span>${esc(m)}</span><b>${number(n)}</b></div>`).join("")}</div>
        <details data-section="usage"><summary>最近调用的消耗与耗时</summary>${v.calls.slice(-8).reverse().map(e => {
          const n = usageFor(e, state), d = e.detail || {};
          return `<div class="obs-call"><span>${esc(d.model || "模型")}</span><b>${n === null ? "未保留单次用量" : `${number(n)} token`}</b><small>${esc(names[d.task_type] || d.task_type || "历史调用未记录用途")} · ${(Number(d.latency_ms || 0) / 1000).toFixed(1)} 秒${d.first_delta_ms == null ? "" : ` · 首次响应 ${(d.first_delta_ms / 1000).toFixed(1)} 秒`} · 缓存 ${typeof d.cached_input_tokens === "number" ? number(d.cached_input_tokens) : "未提供"}${typeof d.reused_prefix_bytes === "number" ? ` · 与前次重复前缀 ${number(d.reused_prefix_bytes)} 字节（本地比较）` : ""}</small></div>`;
        }).join("") || '<p>尚无已返回的模型调用事件</p>'}</details></section>
      <section class="obs-section"><h3>实际产出 <span>证据与判断分开</span></h3>
        <div class="obs-counts"><span><b>${number(v.evidence.length || v.business.observed_endpoints || 0)}</b> 接口 / 页面证据</span>
        <span><b>${number(state.vulns?.total ?? state.vulns?.items?.length ?? 0)}</b> 漏洞记录</span></div>
        <p>已登记 ${number(state.layers?.asset || 0)} 条资产；${v.business.coverage ? "已整理业务上下文。" : "已观测记录先展示，业务分析仍在整理。"}</p>
        ${v.business.coverage ? `<p>${esc(v.business.coverage)}</p>` : ""}
        <details data-section="evidence"><summary>查看已观测接口与页面</summary>${v.evidence.map(e => `<div class="obs-evidence"><strong>${esc(e.method || "GET")} <span>${esc(e.status_code ?? "未记录状态")}</span></strong><code>${esc(e.url || "")}</code><p>${esc(e.note || "")}</p></div>`).join("") || '<p>尚未登记证据；仅有调用不等于已有发现。</p>'}</details>
        <details data-section="facts"><summary>已知事实 · ${(v.business.facts || []).length}</summary>${(v.business.facts || []).map(f => `<p>${esc(typeof f === "string" ? f : f.statement || f.note || "")}</p>`).join("") || '<p>尚无已整理事实</p>'}</details>
        <details data-section="unknowns"><summary>待补信息 · ${((v.business.gaps || v.business.unknowns) || []).length}</summary>${((v.business.gaps || v.business.unknowns) || []).map(x => `<p>${esc(typeof x === "string" ? x : x.statement || x.note || JSON.stringify(x))}</p>`).join("") || '<p>尚未登记</p>'}</details></section>
      <section class="obs-section"><h3>执行情况 <span>${running.length} 个任务在途</span></h3>
        ${tasks.map(t => `<div class="obs-task"><strong>${esc(TASK_CN[t.task_type] || t.agent_type)}</strong><span>${esc(({running:"执行中",done:"已完成",failed:"失败",cancelled:"已撤销",pruned:"已剪枝"})[t.status] || t.status)}</span><p>${esc((t.routes || []).map(r => r.note).filter(Boolean).join("；") || "尚无步骤结果")}</p></div>`).join("") || '<p>尚未派发任务</p>'}
        ${v.failed ? `<button type="button" class="btn sm obs-errors">查看失败原因（${v.failed}）</button>` : '<p>工具结果按真实返回展示，HTTP 成功不代表业务成功。</p>'}
        ${v.recent.map(e => `<div class="obs-recent ${e.detail?.ok === false ? "bad" : ""}"><small>${esc(actorName(e.actor))}</small><p>${esc(resultSummary(e.detail || {}))}</p></div>`).join("")}</section>`;
    for (const el of box.querySelectorAll("details")) if (opened.has(el.dataset.section)) el.open = true;
    const errors = box.querySelector(".obs-errors");
    if (errors) errors.onclick = () => {
      const card = document.querySelector(".tl-act:has(.st.n)");
      if (card) { card.classList.add("open"); card.querySelector("button.hd").setAttribute("aria-expanded", "true"); card.scrollIntoView({block:"center"}); }
    };
    for (const row of document.querySelectorAll(".tl-model[data-call-ts]")) {
      const e = v.calls.find(x => x.ts === row.dataset.callTs);
      const n = e && usageFor(e, state);
      if (n != null) row.querySelector(".model-tokens").textContent = `${number(n)} token`;
    }
    for (const row of document.querySelectorAll(".tl[data-event-ts]")) {
      const clock = row.querySelector("time.t");
      if (clock) clock.textContent = evElapsed(row.dataset.eventTs);
    }
  }
  function push(event) {
    if (event.type !== "agent.delta") {
      events.push(event);
      if (events.length > 1000) events.shift();
    }
    if (!scheduled) { scheduled = true; requestAnimationFrame(render); }
  }
  function reset() {
    events = [];
    const box = $("#session-observer");
    if (box) { box.innerHTML = ""; delete box.dataset.signature; }
  }
  return { render, push, reset, usageFor, resultSummary, derive, toolName, cleanText };
})();
