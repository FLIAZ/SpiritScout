/* ================= 编队 ================= */
let agentSavePending = Promise.resolve();
let agentLoadVersion = 0;
async function loadAgents() {
  const version = ++agentLoadVersion;
  try {
    await agentSavePending;
    const agents = await api("/api/agents");
    if (version !== agentLoadVersion) return;
    S.agents = agents;
    const g = $("#agents-grid");
    g.innerHTML = S.agents.roster.map(a => {
      const bound = S.agents.assignments[a.key] || "";
      const available = a.status === "available";
      const bind = a.configurable ? I18n.html`<div class="bind">
          <select data-agent="${esc(a.key)}" data-orig="${esc(bound)}" aria-label="${esc(I18n.t(a.name))}的模型">
            <option value="">不指派（按模型列表顺序路由）</option>
            ${S.agents.models.map(m => `<option value="${esc(modelKey(m))}" data-prov="${esc(m.provider || "")}" ${modelKey(m) === bound ? "selected" : ""}>${esc(m.model)} · ${esc(m.provider || I18n.t("默认"))}</option>`).join("")}
            ${S.agents.models.length ? "" : I18n.html`<option value="" disabled>（「模型」页还没有启用模型）</option>`}
          </select>
        </div><div class="bind-status muted" role="status" aria-live="polite"></div>` : `<div class="bind-note">${available ? I18n.t("此角色不调用模型") : I18n.t("尚未装配，不参与任务调度")}</div>`;
      return I18n.html`<div class="card acard ${available ? "" : "planned"}">
        <div class="h"><b>${esc(I18n.t(a.name))}</b><span class="en">${esc(a.key)}</span>
          ${available ? "" : I18n.html('<span class="tag orange" style="margin-left:auto">规划中</span>')}</div>
        <div class="duty">${esc(I18n.t(a.duty))}</div>
        <div class="meta-row">${(a.modes || []).map(m => `<span class="tag blue">${esc(MODE_CN[m] || m)}</span>`).join("")}</div>
        <details class="agent-profile-details"><summary>角色说明与工具</summary>
          <p class="mindset">${esc(I18n.t(a.mindset || ""))}</p>
          <ol>${(a.workflow || []).map(step => `<li>${esc(I18n.t(step))}</li>`).join("")}</ol>
          <div class="tools">${(a.tools || []).map(tool => `<span class="tag">${esc(I18n.t(tool))}</span>`).join("")}</div>
        </details>
        ${bind}
      </div>`;
    }).join("");
    const all = $("#bind-all-model");
    all.innerHTML = I18n.html`<option value="">选择统一使用的模型</option>` + S.agents.models.map(m =>
      `<option value="${esc(modelKey(m))}">${esc(m.model)} · ${esc(m.provider || I18n.t("默认"))}</option>`).join("");
    all.disabled = !S.agents.models.length;
    selxSync(all);
    const sels = [...g.querySelectorAll("select[data-agent]")];
    const apply = $("#bind-all-apply");
    apply.disabled = true;
    all.onchange = () => { apply.disabled = !all.value || !sels.length; };
    $("#bind-status").textContent = "";
    // 串行落盘，避免单角色与批量操作交错；保存时同步禁用自定义下拉。
    const save = (targets, model) => {
      const changes = targets.map(sel => ({ sel, model: model ?? sel.value }))
        .filter(item => item.model !== item.sel.dataset.orig);
      if (!changes.length) {
        if (model != null) $("#bind-status").textContent = I18n.t("已应用");
        return Promise.resolve();
      }
      ++agentLoadVersion;
      const controls = [...sels, all];
      controls.forEach(sel => { sel.disabled = true; selxSync(sel); });
      apply.disabled = true;
      $("#bind-status").textContent = I18n.t("保存中…");
      const statusOf = sel => sel.closest(".acard").querySelector(".bind-status");
      changes.forEach(({ sel, model }) => {
        sel.value = model;
        selxSync(sel);
        statusOf(sel).textContent = I18n.t("保存中…");
      });
      agentSavePending = agentSavePending.then(async () => {
        let saved = 0;
        const failed = [];
        for (const { sel, model } of changes) {
          const status = statusOf(sel);
          try {
            S.agents = await api(`/api/agents/${encodeURIComponent(sel.dataset.agent)}`,
              { method: "PUT", body: JSON.stringify({ model }) });
            sel.dataset.orig = S.agents.assignments[sel.dataset.agent] || "";
            sel.value = sel.dataset.orig;
            status.textContent = I18n.t("已保存");
            saved++;
          } catch (e) {
            sel.value = sel.dataset.orig;
            status.textContent = I18n.t("保存失败，已恢复原指派，请重试");
            failed.push(`${sel.dataset.agent}：${e.message}`);
          }
          selxSync(sel);
        }
        $("#bind-status").textContent = failed.length
          ? I18n.t`已保存 ${saved} 个角色，${failed.length} 个失败，请重试`
          : I18n.t`已保存 · ${saved} 个角色`;
        if (failed.length) toast(failed.join("；"), true);
      }).finally(() => {
        controls.forEach(sel => {
          sel.disabled = sel === all && !S.agents.models.length;
          selxSync(sel);
        });
        apply.disabled = !all.value || !sels.length;
      });
      return agentSavePending;
    };
    sels.forEach(sel => { sel.onchange = () => save([sel]); });
    apply.onclick = () => {
      if (!all.value || apply.disabled) return;
      return save(sels, all.value);
    };
  } catch (e) { toast(e.message, true); }
}
