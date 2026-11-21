"use strict";

const AssetIntelligencePage = (() => {
  let loaded = false, generation = 0, providers = new Map();
  const root = () => $("#view-asset-intelligence");
  const names = {fofa:"FOFA",hunter:"Hunter",quake:"Quake",zoomeye:"ZoomEye",aiqicha:"爱企查",qichacha:"企查查"};
  const sourceLabel = value => ({saved:I18n.t("加密资源库"),environment:I18n.t("环境变量"),none:I18n.t("未配置")})[value] || value;

  function shell() {
    root().innerHTML = `<div class="asset-intel-shell">
      <header class="asset-intel-head"><div><p class="asset-intel-kicker">Asset intelligence</p><h2>${I18n.t("资产情报")}</h2><p>${I18n.t("统一管理网络空间测绘与企业归属情报。所有外部结果先进入候选池，不等于授权、归属证明或可直接测试的资产。")}</p></div></header>
      <div class="asset-intel-summary" role="status" aria-live="polite"></div>
      <div class="asset-intel-layout"><main><section class="asset-intel-section"><div class="asset-intel-section-head"><div><p>CYBERSPACE</p><h3>${I18n.t("网络空间测绘")}</h3></div><span>${I18n.t("只保留明确范围内的候选")}</span></div><div class="asset-provider-grid" data-category="cyberspace"></div></section>
      <section class="asset-intel-section"><div class="asset-intel-section-head"><div><p>ENTERPRISE</p><h3>${I18n.t("企业归属与备案")}</h3></div><span>${I18n.t("仅组织线索任务可运行")}</span></div><div data-category="enterprise"></div></section></main>
      <aside class="asset-intel-card"><h3>${I18n.t("候选准入规则")}</h3><p>${I18n.t("发现来源与测试授权严格分离。")}</p><div class="asset-intel-rules">
        <div class="asset-intel-rule scope"><strong>${I18n.t("明确 URL、域名或子域名")}</strong><p>${I18n.t("采用封闭范围，只测试用户逐项提供的目标；测绘结果不得扩散到同公司或其他域名。")}</p></div>
        <div class="asset-intel-rule scope"><strong>${I18n.t("通配符子域")}</strong><p>${I18n.t("FOFA、Hunter、Quake、ZoomEye 与 CT 只补充该后缀下的具体子域，根域不自动进入范围。")}</p></div>
        <div class="asset-intel-rule org"><strong>${I18n.t("公司、省份与控股线索")}</strong><p>${I18n.t("未给出确定域名时，爱企查、企查查等原生 Provider 可按企业、地区、分支、投资和控股比例生成候选并保留归属证据。")}</p></div>
        <div class="asset-intel-rule verify"><strong>${I18n.t("核验后再晋级")}</strong><p>${I18n.t("企业 Cookie 和 API Key 只加密保存在 Vault；候选仍需归属、DNS 与 HTTP 核验。")}</p></div>
      </div></aside></div>
      <dialog class="asset-provider-dialog"><form method="dialog" class="asset-provider-form"><div class="asset-provider-dialog-head"><div><p class="asset-intel-kicker">Provider configuration</p><h3 data-dialog-title></h3></div><button class="icon-btn" value="cancel" aria-label="${I18n.t("关闭")}">×</button></div><div data-dialog-body></div><p class="asset-intel-error" role="alert"></p><div class="asset-intel-actions"><button class="btn primary" type="submit" value="save">${I18n.t("保存配置")}</button><button class="btn" type="button" data-clear>${I18n.t("清除凭据")}</button></div></form></dialog>
    </div>`;
    root().querySelector(".asset-provider-form").addEventListener("submit", save);
    root().querySelector("[data-clear]").addEventListener("click", clearCredentials);
  }

  const statusText = item => !item.enabled ? I18n.t("未启用") : item.key_configured ? I18n.t("已就绪") : I18n.t("待配置");
  const statusClass = item => item.enabled && item.key_configured ? "ready" : "warn";

  function providerCard(item) {
    const detail = `${I18n.t("凭据来源：")}${sourceLabel(item.key_source)}`;
    const description = item.category === "enterprise"
      ? I18n.t("采集企业基本信息、ICP备案、分支和投资关系；只为组织线索任务生成候选。")
      : I18n.t("被动检索已授权域名或通配符后缀，结果必须再次做范围过滤与存活核验。");
    return `<article class="asset-provider-card"><div class="asset-provider-title"><div><span class="asset-provider-mark">${esc(names[item.provider][0])}</span><div><h4>${esc(names[item.provider])}</h4><p>${item.category === "enterprise" ? I18n.t("企业归属") : I18n.t("空间测绘")}</p></div></div><span class="asset-provider-state ${statusClass(item)}">${statusText(item)}</span></div><p>${description}</p><div class="asset-provider-meta"><span>${esc(detail)}</span><span>${item.enabled ? I18n.t("自动候选已开启") : I18n.t("自动候选已关闭")}</span></div><button class="btn" type="button" data-edit="${esc(item.provider)}">${I18n.t("配置 Provider")}</button></article>`;
  }

  function paint(data) {
    providers = new Map(data.providers.map(item => [item.provider, item]));
    const ready = data.providers.filter(item => statusClass(item) === "ready").length;
    root().querySelector(".asset-intel-summary").innerHTML = `<span class="ready">${ready} ${I18n.t("个 Provider 就绪")}</span><span>${data.providers.length} ${I18n.t("个来源")}</span><span>${I18n.t("凭据不回显、不进入浏览器存储")}</span>`;
    for (const category of ["cyberspace","enterprise"]) root().querySelector(`[data-category="${category}"]`).innerHTML = data.providers.filter(item=>item.category===category).map(providerCard).join("");
    root().querySelectorAll("[data-edit]").forEach(button => button.addEventListener("click", () => edit(button.dataset.edit)));
  }

  function edit(provider) {
    const item = providers.get(provider), dialog = root().querySelector("dialog");
    dialog.dataset.provider = provider; root().querySelector("[data-dialog-title]").textContent = names[provider];
    const body = root().querySelector("[data-dialog-body]");
    if (provider === "fofa") body.innerHTML = `<label class="asset-intel-toggle"><input name="enabled" type="checkbox" ${item.enabled?"checked":""}>${I18n.t("启用候选检索")}</label><label>${I18n.t("FOFA 邮箱")}<input name="email" type="email" autocomplete="username" placeholder="name@example.com"></label><label>${I18n.t("FOFA API Key")}<input name="api_key_value" type="password" autocomplete="new-password" placeholder="••••••••"></label>`;
    else if (item.category === "enterprise") body.innerHTML = `<label class="asset-intel-toggle"><input name="enabled" type="checkbox" ${item.enabled?"checked":""}>${I18n.t("启用企业资产收集")}</label><label>${esc(names[provider])} Cookie<textarea name="cookie_value" rows="5" autocomplete="off" placeholder="Cookie: ..."></textarea></label><p class="asset-provider-hint">${I18n.t("灵枢原生 Provider 会提取企业关系与 ICP 域名；验证码或登录失效会明确报告。")}</p>`;
    else body.innerHTML = `<label class="asset-intel-toggle"><input name="enabled" type="checkbox" ${item.enabled?"checked":""}>${I18n.t("启用候选检索")}</label><label>${esc(names[provider])} API Key<input name="api_key_value" type="password" autocomplete="new-password" placeholder="••••••••"></label><p class="asset-provider-hint">${I18n.t("保存后只显示已配置状态，不回显密钥。")}</p>`;
    root().querySelector(".asset-intel-error").textContent = "";
    root().querySelector("[data-clear]").disabled = !item.stored_credentials;
    dialog.showModal();
  }

  async function load() {
    const ticket = ++generation; if (!loaded) { shell(); loaded = true; }
    try { const data = await api("/api/resources/assets/providers"); if (ticket === generation) paint(data); }
    catch (error) { if (ticket === generation) root().querySelector(".asset-intel-summary").textContent = error.message; }
  }

  async function save(event) {
    event.preventDefault(); const dialog = root().querySelector("dialog"), provider = dialog.dataset.provider, item = providers.get(provider), form = event.currentTarget;
    if (event.submitter?.value === "cancel") { dialog.close(); return; }
    const data = new FormData(form), payload = {enabled:data.get("enabled")==="on", expected_version:item.version};
    if (provider === "fofa") { const email=String(data.get("email")||"").trim(), key=String(data.get("api_key_value")||"").trim(); if(email||key){payload.email=email;payload.api_key_value=key;} }
    else if (item.category === "enterprise") { const cookie=String(data.get("cookie_value")||"").trim(); if(cookie)payload.cookie_value=cookie; }
    else { const key=String(data.get("api_key_value")||"").trim(); if(key)payload.api_key_value=key; }
    try { await api(provider === "fofa" ? "/api/resources/assets/fofa" : `/api/resources/assets/providers/${provider}`, {method:"PUT",body:JSON.stringify(payload)}); dialog.close(); await load(); toast(I18n.t("配置已保存")); }
    catch(error){ root().querySelector(".asset-intel-error").textContent=error.message; }
  }

  async function clearCredentials() {
    const dialog=root().querySelector("dialog"), provider=dialog.dataset.provider, item=providers.get(provider), form=root().querySelector(".asset-provider-form"), data=new FormData(form);
    const payload={enabled:false,clear_credentials:true,expected_version:item.version};
    try { await api(provider === "fofa" ? "/api/resources/assets/fofa" : `/api/resources/assets/providers/${provider}`, {method:"PUT",body:JSON.stringify(payload)}); dialog.close(); await load(); toast(I18n.t("凭据已清除")); }
    catch(error){ root().querySelector(".asset-intel-error").textContent=error.message; }
  }

  function navigate(view) { if (view === "asset-intelligence") load(); else generation += 1; }
  return {navigate, load};
})();
