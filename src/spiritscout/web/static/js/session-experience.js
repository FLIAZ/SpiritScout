"use strict";
const SessionExperience = (() => {
  let flight = null, generation = 0;
  function cards(items) {
    return items.map(item => `<article class="exp-card" data-insight="${esc(item.insight_id || "")}"><div class="exp-card-tags">${(item.tags || []).map(tag => `<span class="exp-ins-tag">${esc(tag)}</span>`).join("")}<span class="muted">${item.merged_into ? I18n.t("已整理原件") : item.quality_status === "referenced" ? I18n.t("待核验 · 有来源") : I18n.t("待核验")}</span></div><p class="exp-insight">${esc(item.insight)}</p>${item.context ? I18n.html`<p class="exp-context">适用：${esc(item.context)}</p>` : ""}${item.sources?.length ? I18n.html`<details><summary>来源依据（${item.sources.length} 条）</summary><ul>${item.sources.map(source => `<li>${esc(source.source_session)} · ${esc(source.ref)} · ${esc(source.evidence_state || I18n.t("未核验"))}</li>`).join("")}</ul></details>` : ""}<footer>${esc(modelDisplayName(item.source_model || ""))}<span>${esc(item.source_session || I18n.t("来源见记录"))}</span></footer></article>`).join("");
  }
  function historyRows(rows) {
    const labels = {ok: I18n.t("已入库"), duplicate: I18n.t("与已有经验重复"), empty: I18n.t("没有新经验"), unparsed: I18n.t("结果无法解析"), error: I18n.t("提炼失败"), skipped: I18n.t("已跳过")};
    return rows.map(row => I18n.html`<article class="exp-hist-row"><strong>${esc(labels[row.result] || row.result || I18n.t("处理中"))}</strong><p>${esc(row.trigger === "manual" ? I18n.t("手动提炼") : I18n.t("自动提炼"))} · 新增 ${Number(row.insights || 0)} 条${row.duplicates ? I18n.t` · 跳过重复 ${Number(row.duplicates)} 条` : ""}</p>${row.error || row.reason ? `<p>${esc(I18n.t(row.error || row.reason))}</p>` : ""}<small>${esc(row.ts || "")}</small></article>`).join("");
  }
  async function loadOnce() {
    if (S.tab !== "experience" || !S.cur) return;
    const id = S.cur, version = ++generation, root = $("#tab-experience");
    try {
      const data = await api(`/api/sessions/${encodeURIComponent(id)}/experience`);
      if (version !== generation || S.cur !== id || S.tab !== "experience") return;
      const signature=JSON.stringify([id,data]);
      if(root._content===signature)return;
      const same=root.dataset.session===id,history=root.querySelector('.exp-history');
      const top=same?history?.scrollTop || 0:0,left=same?history?.scrollLeft || 0:0;
      const opened=new Set(same?[...root.querySelectorAll('[data-insight]')].filter(card=>card.querySelector('details')?.open).map(card=>card.dataset.insight):[]);
      root.innerHTML = I18n.html`<header class="console-insight-head"><div><h3>会话经验</h3></div><button class="btn" data-distill ${data.state?.can_distill ? "" : "disabled"}>${data.state?.distilling ? I18n.t("提炼中…") : I18n.t("提炼")}</button></header><p class="muted" role="status">${esc(I18n.t(data.state?.reason || ""))}</p><div class="exp-layout"><section>${cards(data.items || []) || I18n.html('<p class="muted">这次会话尚未产出经验。</p>')}</section><aside class="exp-history"><h3>提炼记录</h3>${historyRows(data.history || []) || I18n.html('<p class="muted">尚无提炼记录。</p>')}</aside></div>`;
      root._content=signature;root.dataset.session=id;
      root.querySelectorAll('[data-insight]').forEach(card=>{if(opened.has(card.dataset.insight)&&card.querySelector('details'))card.querySelector('details').open=true;});
      const nextHistory=root.querySelector('.exp-history');nextHistory.scrollTop=top;nextHistory.scrollLeft=left;
      root.querySelector("[data-distill]").onclick = async event => {
        const button=event.currentTarget;button.disabled = true;
        try {
          await api(`/api/sessions/${encodeURIComponent(id)}/experience/distill`, {method: "POST"});
          toast(I18n.t("已加入后台提炼队列"));
        } catch (error) { toast(I18n.t`未开始提炼：${error.message}`, true); }
        finally { if (S.cur === id) {button.disabled=!data.state?.can_distill;load();} }
      };
    } catch (error) {
      if (S.cur === id && S.tab === "experience" && version === generation) { delete root._content;root.innerHTML = I18n.html`<p role="alert">读取失败：${esc(error.message)}</p><button class="btn" data-retry>重试</button>`; }
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
