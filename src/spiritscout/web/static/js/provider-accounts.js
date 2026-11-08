/* 供应商账户：价格公开查询、密钥只留后端、额度与任务消耗分开显示。 */
const ProviderAccounts = (() => {
  const labels = { manual: I18n.t("自定义报价"), glm_api: I18n.t("GLM 按量"), glm_token_plan: I18n.t("GLM 套餐"),
    kimi_api: I18n.t("Kimi 按量"), kimi_token_plan: I18n.t("Kimi 套餐"), deepseek_api: I18n.t("DeepSeek 按量"), other_token_plan: I18n.t("套餐订阅") };
  const endpoints = { glm_api: "https://open.bigmodel.cn/api/paas/v4", glm_token_plan: "https://open.bigmodel.cn/api/coding/paas/v4",
    kimi_api: "https://api.moonshot.cn/v1", kimi_token_plan: "https://api.kimi.com/coding/v1", deepseek_api: "https://api.deepseek.com" };
  let priceVersion = 0, flow = null, pollTimer = null;
  const pending = new Set(), snapshots = new Map();
  let generation = 0, usagePending = false;
  const timeLabel = ts => ts ? new Date(ts * 1000).toLocaleString(I18n.dateLocale) : I18n.t("尚未查询");
  const num = value => Number(value).toLocaleString(I18n.dateLocale, { maximumFractionDigits: 2 });
  const url = name => `/api/providers/${encodeURIComponent(name)}`;
  const quotaNodes = name => [...document.querySelectorAll("[data-provider-quota]")].filter(el => el.dataset.providerQuota === name);
  const finite = n => typeof n === "number" && Number.isFinite(n);
  const compact = n => n >= 1e6 ? `${num(n / 1e6)}M` : n >= 1000 ? `${num(n / 1000)}k` : num(n);
  function quotaLabel(value) {
    const hours = /^(\d+) 小时额度$/.exec(value);
    if (hours) return I18n.t`${hours[1]} 小时额度`;
    const window = /^短周期额度 (\d+)$/.exec(value);
    return window ? I18n.t`短周期额度 ${window[1]}` : I18n.t(value);
  }
  function countdown(value) {
    const end = Date.parse(value);
    if (!Number.isFinite(end)) return "";
    const minutes = Math.ceil((end - Date.now()) / 60000);
    if (minutes <= 0) return I18n.t("待刷新");
    if (minutes >= 1440) return `${Math.floor(minutes / 1440)}d ${Math.floor(minutes % 1440 / 60)}h`;
    return minutes >= 60 ? `${Math.floor(minutes / 60)}h ${minutes % 60}m` : `${minutes}m`;
  }

  function quotaText(q) {
    if (!q) return I18n.t("尚未查询");
    const values = (q.balances || []).map(b => I18n.t`余额 ${num(b.remaining)} ${b.currency}`
      + (b.granted != null ? I18n.t`（赠金 ${num(b.granted)}，充值 ${b.topped_up == null ? I18n.t("未知") : num(b.topped_up)}）` : ""));
    for (const w of q.windows || []) {
      values.push(I18n.t`${quotaLabel(w.label)}剩余 ${num(w.remaining_percent)}%`
        + (w.resets_at ? I18n.t` · ${new Date(w.resets_at).toLocaleString(I18n.dateLocale)} 重置` : ""));
    }
    if (q.status !== "ok") values.unshift(I18n.t(q.message) || I18n.t("额度暂未查到"));
    if (q.stale) values.push(I18n.t("以上为上次查询值"));
    if (q.fetched_at) values.push(I18n.t`更新于 ${timeLabel(q.fetched_at)}`);
    return values.join("；") || I18n.t("额度暂未查到");
  }

  function controls(p) {
    return p.auth_method === "kimi_oauth" ? `<button class="btn sm" data-pact="login" data-name="${esc(p.name)}">${p.key_configured ? I18n.t("重新登录") : I18n.t("登录 Kimi")}</button>
      ${p.key_configured ? I18n.html`<button class="btn sm" data-pact="logout" data-name="${esc(p.name)}">退出账号</button>` : ""}` : "";
  }

  function modelUsage(m) {
    return I18n.html`<div class="model-usage" data-model-usage="${esc(modelKey(m))}" aria-live="polite"><span>本机累计</span><span class="muted">读取中…</span></div>`;
  }

  function quotaSlot(name) {
    return I18n.html`<button type="button" class="model-quota" data-provider-quota="${esc(name)}" data-pact="quota" data-name="${esc(name)}"
      aria-label="刷新 ${esc(name)} 的共享余额与额度" title="同一供应商账户下的模型共享额度；点击刷新"><span class="quota-caption">账户共享</span><span>查询中…</span></button>`;
  }

  function quotaHTML(q) {
    if (!q) return I18n.html('<span class="quota-caption">账户共享</span><span>查询中…</span>');
    const rows = (q.balances || []).filter(b => finite(b.remaining)).map(b =>
      I18n.html`<span class="quota-line">余额 <b class="${b.remaining <= 0 ? "quota-low" : "quota-good"}">${num(b.remaining)} ${esc(b.currency)}</b></span>`);
    for (const w of q.windows || []) {
      if (!finite(w.remaining_percent)) continue;
      const reset = countdown(w.resets_at);
      rows.push(I18n.html`<span class="quota-line">${esc(quotaLabel(w.label).replace(/额度$/, ""))}剩余 <b class="${w.remaining_percent <= 10 ? "quota-low" : "quota-good"}">${num(w.remaining_percent)}%</b>${reset ? I18n.html` <span class="quota-reset" title="${esc(w.resets_at)} 重置">◷ ${esc(reset)}</span>` : ""}</span>`);
    }
    const state = { unsupported: I18n.t("暂不支持查询"), disabled: I18n.t("查询已关闭"), auth_required: I18n.t("需配置查询凭据"), unavailable: I18n.t("暂时无法查询") };
    return I18n.html`<span class="quota-caption">账户共享${q.stale ? I18n.t(" · 上次结果") : ""}</span>`
      + (rows.join("") || `<span>${esc(state[q.status] || I18n.t("尚无额度数据"))}</span>`);
  }

  function paint(name) {
    const q = snapshots.get(name);
    for (const node of quotaNodes(name)) {
      node.innerHTML = quotaHTML(q);
      node.title = I18n.t`${name} · 账户共享，非单模型独立余额\n${quotaText(q)}\n点击刷新`;
      node.classList.toggle("quota-stale", !!q?.stale);
      node.setAttribute("aria-busy", String(pending.has(name)));
    }
  }

  async function refreshUsage() {
    if (usagePending) return;
    usagePending = true;
    try {
      const data = await api("/api/usage/stats?period=all");
      // 只按供应商 + 模型的稳定身份匹配；历史裸模型名不能猜归属。
      const rows = new Map((data.models || []).filter(r => r.provider).map(r => [r.model_key, r]));
      for (const node of document.querySelectorAll("[data-model-usage]")) {
        const r = rows.get(node.dataset.modelUsage);
        const incomplete = data.source_errors || r?.usage_incomplete_calls;
        const hit = r?.cache?.hit_rate;
        node.innerHTML = I18n.html`<span>本机累计 <b>${r ? compact(r.tokens) : data.source_errors ? "—" : "0"}</b>${r ? " tokens" : ""}${incomplete ? " *" : ""}</span>`
          + `<span class="muted">${r ? I18n.t`${num(r.calls)} 次` : I18n.t("暂无归属记录")}${finite(hit) ? I18n.t` · 缓存 ${num(hit * 100)}%${r.cache.unmeasured_calls ? " *" : ""}` : ""}</span>`;
        node.title = I18n.t`灵枢本机累计记录，不代表供应商账户全部消耗。${incomplete ? I18n.t("部分调用用量未上报或账本读取失败，统计不完整。") : ""}${r ? I18n.t`\n已记录 ${num(r.tokens)} tokens` : I18n.t("历史未标明供应商的记录不分配到当前模型。")}${r?.cache?.unmeasured_calls ? I18n.t`\n缓存比例仅来自已测调用；${num(r.cache.unmeasured_calls)} 次未返回缓存用量。` : ""}`;
      }
    } catch {
      for (const node of document.querySelectorAll("[data-model-usage]")) {
        if (node.textContent.includes(I18n.t("读取中"))) node.innerHTML = I18n.html('<span>本机用量</span><span class="muted">暂时无法读取</span>');
        node.title = I18n.t("本机用量刷新失败，已有数字为上次结果");
      }
    } finally { usagePending = false; }
  }

  async function refresh(name, force = true) {
    if (pending.has(name)) return;
    if (!quotaNodes(name).length) return;
    const active = generation;
    pending.add(name);
    paint(name);
    try {
      const q = await api(`${url(name)}/quota?refresh=${force ? "true" : "false"}`);
      if (active === generation) snapshots.set(name, q);
    } catch (e) {
      if (active === generation) { const old = snapshots.get(name); snapshots.set(name, { ...old, status: "unavailable", stale: !!old?.fetched_at, message: I18n.t`查询失败：${e.message}` }); }
    } finally { if (active === generation) { pending.delete(name); paint(name); } }
  }

  function refreshVisible() {
    if (document.hidden || !$("#providers-list")?.offsetParent) return;
    for (const p of S.providers || []) refresh(p.name, false);
    refreshUsage();
  }

  async function prices(force = false) {
    const generation = ++priceVersion, scheme = $("#p-billing").value, box = $("#p-price");
    $("#p-price-refresh").disabled = ["manual", "other_token_plan"].includes(scheme);
    $("#p-use-endpoint").disabled = !endpoints[scheme];
    $("#p-plan").disabled = !scheme.endsWith("_token_plan");
    $("#p-plan-options").innerHTML = "";
    $("#p-auth").querySelector('[value="kimi_oauth"]').disabled = scheme !== "kimi_token_plan";
    if (scheme !== "kimi_token_plan" && $("#p-auth").value === "kimi_oauth") $("#p-auth").value = "api_key";
    authNote();
    if (scheme === "manual") { box.textContent = I18n.t("沿用各模型的自定义美元单价；未配置的价格记为未知。"); return; }
    if (scheme === "other_token_plan") { box.textContent = I18n.t("套餐费与任务用量分开统计，不按 API 单价分摊。当前尚无自动价格适配，费用以厂商结算页为准。"); return; }
    box.textContent = I18n.t("正在查询官方价格…");
    try {
      const q = await api(`/api/provider-pricing/${encodeURIComponent(scheme)}?refresh=${force ? "true" : "false"}`);
      if (generation !== priceVersion) return;
      const sources = (q.sources || []).filter(u => /^https:\/\//.test(u)).map((u, i) => I18n.html`<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">来源 ${i + 1}</a>`).join(" · ");
      let body = `<div>${q.status === "fresh" ? I18n.t("官方参考价格") : I18n.t("价格未更新")} · ${esc(timeLabel(q.fetched_at))} · ${sources}</div>`;
      if (q.error) body += `<p>${esc(q.error)}</p>`;
      if (q.kind === "subscription") {
        const plans = Object.entries(q.plans || {});
        $("#p-plan-options").innerHTML = plans.map(([name]) => `<option value="${esc(name)}">`).join("");
        body += I18n.html`<p>套餐订阅费单独计费，任务记录 Token 用量，不按 API 单价摊算订阅费。</p>`;
        body += plans.map(([name, p]) => `<div><b>${esc(name)}</b>：${[
          p.month != null ? I18n.t`${num(p.month)} 元/月` : "",
          p.quarter != null ? I18n.t`${num(p.quarter)} 元/季` : "",
          p.year != null ? I18n.t`${num(p.year)} 元/年` : "",
          p.year_monthly_equivalent != null ? I18n.t`年付折算 ${num(p.year_monthly_equivalent)} 元/月` : "",
          p.quota_5h != null ? I18n.t`5 小时 ${num(p.quota_5h)} 积分` : "",
          p.quota_week != null ? I18n.t`每周 ${num(p.quota_week)} 积分` : ""].filter(Boolean).join(" · ")}</div>`).join("");
        if (q.fee_status === "checkout_required") body += I18n.html("<p>订阅费需到官方结算页确认。</p>");
      } else {
        body += I18n.html`<details><summary>查看各模型单价（元 / 百万 Token）</summary><table><thead><tr><th>模型 / 条件</th><th>输入</th><th>缓存读</th><th>输出</th></tr></thead><tbody>`;
        for (const [model, rates] of Object.entries(q.models || {})) for (const r of rates)
          body += `<tr><td>${esc(model)}${r.condition || r.period ? `<br>${esc(r.condition || ({ peak: I18n.t("高峰"), off_peak: I18n.t("空闲") }[r.period] || r.period))}` : ""}</td><td>${num(r.input)}</td><td>${r.cached_input == null ? I18n.t("未报价") : num(r.cached_input)}</td><td>${num(r.output)}</td></tr>`;
        body += I18n.html("</tbody></table></details><p>按调用时价格快照估算；折扣、代理及资源包请使用自定义报价。</p>");
      }
      if (q.note) body += `<p>${esc(q.note)}</p>`;
      box.innerHTML = body;
    } catch (e) { if (generation === priceVersion) box.textContent = I18n.t`价格查询失败：${e.message}`; }
  }

  function authNote() {
    const oauth = $("#p-auth").value === "kimi_oauth", plan = $("#p-billing").value.endsWith("_token_plan");
    $("#p-keyval").disabled = oauth;
    queryCredentials();
    for (const id of ["#p-auth", "#p-billing", "#p-quota", "#p-type"]) selxSync($(id));
    $("#p-auth-note").textContent = oauth
      ? I18n.t("保存后在模型页点击“登录 Kimi”。访问令牌与刷新令牌加密保存在本机，失效后可重新登录。")
      : plan ? I18n.t("使用套餐专用 Key 和套餐端点；标准 API Key 与套餐 Key 可能不通用。是否支持灵枢及额度扣除以供应商账户权益为准。")
      : I18n.t("API Key 由后端加密保管；余额与额度查询不调用模型。");
  }

  function fill(p) {
    $("#p-billing").value = p.billing_scheme || "manual";
    $("#p-plan").value = p.billing_plan || "";
    $("#p-auth").value = p.auth_method || "api_key";
    $("#p-quota").value = p.quota_query || "auto";
    $("#p-quota-ak").value = ""; $("#p-quota-sk").value = ""; $("#p-quota-key-clear").checked = false;
    $("#p-quota-key-status").textContent = p.quota_credentials_configured ? I18n.t("查询凭据已加密保存") : I18n.t("尚未配置查询凭据");
    prices();
  }

  function queryCredentials() {
    let ark = $("#p-quota").value === "ark_plan";
    try { ark ||= $("#p-quota").value === "auto" && new URL($("#p-baseurl").value).hostname === "ark.cn-beijing.volces.com"; } catch { /* 自定义地址可能尚未填完。 */ }
    $("#p-query-credentials").hidden = !ark;
  }

  function fields() {
    const ak = $("#p-quota-ak").value.trim(), sk = $("#p-quota-sk").value.trim();
    return { billing_scheme: $("#p-billing").value, billing_plan: $("#p-plan").disabled ? "" : $("#p-plan").value.trim(),
      auth_method: $("#p-auth").value, quota_query: $("#p-quota").value,
      ...(!$("#p-query-credentials").hidden && ($("#p-quota-key-clear").checked || ak || sk)
        ? { quota_credentials: $("#p-quota-key-clear").checked ? {} : { access_key_id: ak, secret_access_key: sk } } : {}),
      ...($("#p-auth").value === "kimi_oauth" ? { api_format: "chat_completions" } : {}) };
  }

  async function checkLogin() {
    if (!flow) return;
    const active = flow;
    try {
      const result = await api(`${url(active.name)}/oauth/poll`, { method: "POST", body: JSON.stringify({ flow_id: active.flow_id }) });
      if (flow !== active) return;
      if (result.status === "authenticated") {
        flow = null;
        $("#provider-oauth-status").textContent = I18n.t("已登录，凭据已加密保存。可以获取模型列表并查询套餐额度。");
        loadModels();
        return;
      }
      if (["expired", "denied"].includes(result.status)) {
        flow = null;
        $("#provider-oauth-status").textContent = result.status === "expired" ? I18n.t("授权码已过期，请关闭后重新登录。") : I18n.t("授权已取消。");
        return;
      }
      if (Date.now() >= active.expires_at * 1000) { flow = null; $("#provider-oauth-status").textContent = I18n.t("授权码已过期，请重新登录。"); return; }
      pollTimer = setTimeout(checkLogin, Math.max(5, result.interval || 5) * 1000);
    } catch (e) {
      if (flow !== active) return;
      $("#provider-oauth-error").textContent = I18n.t`查询暂未成功：${e.message}`;
      if (Date.now() < active.expires_at * 1000) pollTimer = setTimeout(checkLogin, 10000);
    }
  }

  async function login(name) {
    if (flow) return;
    $("#provider-oauth-dlg").showModal();
    $("#provider-oauth-status").textContent = I18n.t("正在创建设备授权…");
    try {
      const result = await api(`${url(name)}/oauth/start`, { method: "POST", body: "{}" });
      if (!$("#provider-oauth-dlg").open) { await api(`${url(name)}/oauth?cancel_only=true`, { method: "DELETE" }); return; }
      flow = { ...result, name };
      $("#provider-oauth-status").innerHTML = I18n.html`<p>在 Kimi 官方页面完成授权，然后回到这里。</p>
        <p><a class="btn primary" href="${esc(result.verification_uri)}" target="_blank" rel="noopener noreferrer">打开 Kimi 授权页面</a></p>
        <p>设备码：<strong class="mono">${esc(result.user_code)}</strong></p><p>有效期至 ${esc(timeLabel(result.expires_at))}</p><p id="provider-oauth-error" aria-live="polite"></p>`;
      pollTimer = setTimeout(checkLogin, result.interval * 1000);
    } catch (e) { $("#provider-oauth-status").textContent = I18n.t`登录未开始：${e.message}`; }
  }

  async function closeLogin() {
    clearTimeout(pollTimer);
    const old = flow; flow = null;
    $("#provider-oauth-dlg").close();
    if (old) try { await api(`${url(old.name)}/oauth?cancel_only=true`, { method: "DELETE" }); } catch { /* 超时流程会自动过期。 */ }
  }

  async function logout(name) {
    try { await api(`${url(name)}/oauth`, { method: "DELETE" }); toast(I18n.t("已清除本机 OAuth 登录")); loadModels(); }
    catch (e) { toast(e.message, true); }
  }

  $("#p-billing").onchange = () => { $("#p-plan").value = ""; prices(); };
  $("#p-quota").onchange = queryCredentials;
  $("#p-baseurl").addEventListener("input", queryCredentials);
  $("#p-auth").onchange = () => {
    if ($("#p-auth").value === "kimi_oauth") {
      $("#p-baseurl").value = endpoints.kimi_token_plan; $("#p-type").value = "openai_compatible";
    }
    authNote();
  };
  $("#p-price-refresh").onclick = () => prices(true);
  $("#p-use-endpoint").onclick = () => {
    const endpoint = endpoints[$("#p-billing").value];
    if (endpoint) { $("#p-baseurl").value = endpoint; $("#p-type").value = "openai_compatible"; selxSync($("#p-type")); }
  };
  $("#provider-oauth-close").onclick = closeLogin;
  $("#provider-oauth-dlg").addEventListener("cancel", event => { event.preventDefault(); closeLogin(); });
  setInterval(refreshVisible, 60000);
  setInterval(() => { if (!document.hidden) for (const name of snapshots.keys()) paint(name); }, 30000);
  function reset() { generation++; pending.clear(); snapshots.clear(); }
  return { controls, modelUsage, quotaSlot, fill, fields, refresh, refreshVisible, login, logout, quotaText, quotaHTML, countdown, reset };
})();
