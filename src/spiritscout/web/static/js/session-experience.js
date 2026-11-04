"use strict";
const SessionExperience = (() => {
  let flight = null, generation = 0;
  function cards(items) {
    return items.map(item => `<article class="exp-card"><div class="exp-card-tags">${(item.tags || []).map(tag => `<span class="exp-ins-tag">${esc(tag)}</span>`).join("")}<span class="muted">${item.merged_into ? "已整理原件" : item.quality_status === "referenced" ? "待核验 · 有来源" : "待核验"}</span></div><p class="exp-insight">${esc(item.insight)}</p>${item.context ? `<p class="exp-context">适用：${esc(item.context)}</p>` : ""}${item.sources?.length ? `<details><summary>来源依据（${item.sources.length} 条）</summary><ul>${item.sources.map(source => `<li>${esc(source.source_session)} · ${esc(source.ref)} · ${esc(source.evidence_state || "未核验")}</li>`).join("")}</ul></details>` : ""}<footer>${esc(modelDisplayName(item.source_model || ""))}<span>${esc(item.source_session || "来源见记录")}</span></footer></article>`).join("");
  }
  function historyRows(rows) {
    const labels = {ok: "已入库", duplicate: "与已有经验重复", empty: "没有新经验", unparsed: "结果无法解析", error: "提炼失败", skipped: "已跳过"};
    return rows.map(row => `<article class="exp-hist-row"><strong>${esc(labels[row.result] || row.result || "处理中")}</strong><p>${esc(row.trigger === "manual" ? "手动提炼" : "自动提炼")} · 新增 ${Number(row.insights || 0)} 条${row.duplicates ? ` · 跳过重复 ${Number(row.duplicates)} 条` : ""}</p>${row.error || row.reason ? `<p>${esc(row.error || row.reason)}</p>` : ""}<small>${esc(row.ts || "")}</small></article>`).join("");
  }
  async function loadOnce() {
    if (S.tab !== "experience" || !S.cur) return;
    const id = S.cur, version = ++generation, root = $("#tab-experience");
    try {
      const data = await api(`/api/sessions/${encodeURIComponent(id)}/experience`);
      if (version !== generation || S.cur !== id || S.tab !== "experience") return;
      root.innerHTML = `<header class="console-insight-head"><div><h3>会话经验</h3></div><button class="btn" data-distill ${data.state?.can_distill ? "" : "disabled"}>${data.state?.distilling ? "提炼中…" : "提炼"}</button></header><p class="muted" role="status">${esc(data.state?.reason || "")}</p><div class="exp-layout"><section>${cards(data.items || []) || '<p class="muted">这次会话尚未产出经验。</p>'}</section><aside class="exp-history"><h3>提炼记录</h3>${historyRows(data.history || []) || '<p class="muted">尚无提炼记录。</p>'}</aside></div>`;
      root.querySelector("[data-distill]").onclick = async event => {
        event.currentTarget.disabled = true;
        try {
          await api(`/api/sessions/${encodeURIComponent(id)}/experience/distill`, {method: "POST"});
          toast("已加入后台提炼队列");
        } catch (error) { toast(`未开始提炼：${error.message}`, true); }
        finally { if (S.cur === id) load(); }
      };
    } catch (error) {
      if (S.cur === id && version === generation) root.innerHTML = `<p role="alert">读取失败：${esc(error.message)}</p><button class="btn" data-retry>重试</button>`;
      root.querySelector("[data-retry]")?.addEventListener("click", load);
    }
  }
  function load() {
    if (flight && flight.id === S.cur) return flight.promise;
    const marker = {id: S.cur};
    marker.promise = loadOnce().finally(() => { if (flight === marker) flight = null; });
    flight = marker;
    return marker.promise;
  }
  return {load, cards, historyRows};
})();
