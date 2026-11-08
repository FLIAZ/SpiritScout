const modelKey = entry => entry.key || entry.model;
const modelLabel = (id, catalog) => {
  const entry = catalog ? catalog.byKey.get(id) : (S.models?.models || []).find(m => modelKey(m) === id);
  return entry ? `${entry.provider_ref || I18n.t("默认")} / ${entry.model}` : modelDisplayName(id);
};

/* 每次渲染创建索引，保留原列表顺序；配置刷新后不会复用旧数据。 */
function indexModels(models) {
  const byKey = new Map(), byProvider = new Map();
  for (const model of models) {
    const key = modelKey(model), provider = model.provider_ref || "";
    if (!byKey.has(key)) byKey.set(key, model);
    if (!byProvider.has(provider)) byProvider.set(provider, []);
    byProvider.get(provider).push(model);
  }
  return {byKey, byProvider};
}
/* ================= 模型 ================= */
/* 供应商（一个 API 地址 + 一把密钥）下挂模型，模型 ID 即身份。密钥由服务端
   密钥库加密保管（工作区 vault.db），页面不缓存、不回传。 */
const PROVIDER_CN = { openai_compatible: I18n.t("OpenAI 兼容"), anthropic_compatible: I18n.t("Anthropic 兼容"), ollama: I18n.t("Ollama 原生"), mock: I18n.t("离线演练") };
const fmtCtx = n => n > 0 ? (n >= 1000 ? Math.round(n / 1000) + "k" : String(n)) : "—";
/* 模型/供应商的操作按钮统一 data-* 化：动态值只进 HTML 属性（esc 转义），
   不再拼内联 onclick 字符串——值里的引号、尖括号从此进不了 JS 语境 */
const M_ICONS = {
  file: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>`,
  sync: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.7-6.4"/><path d="M21 3v6h-6"/></svg>`,
  pencil: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z"/></svg>`,
  trash: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>`,
  plus: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>`,
  hub: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="3"/><circle cx="4.5" cy="5" r="1.8"/><circle cx="19.5" cy="5" r="1.8"/><circle cx="12" cy="20" r="1.8"/><path d="M9.8 10.2 6 6.3M14.2 10.2 18 6.3M12 15v3.2"/></svg>`,
};

function keyLed(p) {
  if (p.auth_method === "kimi_oauth")
    return `<span class="pb-key ${p.key_configured && p.auth_status !== "reauth_required" ? "ok" : "bad"}">${p.key_configured && p.auth_status !== "reauth_required" ? I18n.t("OAuth 已连接") : I18n.t("OAuth 待登录")}</span>`;
  if (p.key_source === "vault")
    return I18n.html`<span class="led ok" title="密钥已加密保存在本机，重启后仍然有效"></span><span class="pb-key ok">密钥已就绪</span>`;
  return I18n.html`<span class="led bad" title="编辑该供应商填入密钥"></span><span class="pb-key bad">密钥未配置</span>`;
}

function modelTile(m, catalog) {
  const caps = (m.capabilities || []).map(c => `<span class="tag">${esc(c)}</span>`).join("");
  const eff = m.reasoning_effort_levels || [];
  const q = m.quality_score;
  return I18n.html`<div class="mtile model-row">
    <div class="model-summary">
    <div class="mt-id"><b>${esc(m.model)}</b></div>
    <div class="mt-caps">${caps}</div>
    <div class="model-meta">上下文 ${fmtCtx(m.context_window)}${q == null ? "" : I18n.t` · 质量 ${esc(q)}`}</div>
    <div class="mt-eff"><span class="k">思考强度</span>${eff.length
      ? eff.map(e => `<span class="eff-pill">${esc(e)}</span>`).join("")
      : I18n.html`<span class="muted" style="font-size:11.5px">默认档</span>`}</div>
    ${(m.is_fallback_for || []).length ? I18n.html`<div class="mt-fb">备：${esc(m.is_fallback_for.map(id => modelLabel(id, catalog)).join("、"))}</div>` : ""}
    </div>
    ${ProviderAccounts.modelUsage(m)}
    ${ProviderAccounts.quotaSlot(m.provider_ref || "")}
    <div class="mt-ops">
      <button data-mact="test" data-model="${esc(modelKey(m))}">测试</button>
      <button data-mact="edit" data-model="${esc(modelKey(m))}">编辑</button>
      <button class="danger" data-mact="del" data-model="${esc(modelKey(m))}">删除</button>
    </div>
  </div>`;
}

async function loadModels() {
  try {
    const [pv, md, nm] = await Promise.all([api("/api/providers"), api("/api/models"),
                                            api("/api/models/naming").catch(() => null),
                                            loadPresets()]);
    S.providers = pv.providers; S.models = md;
    const catalog = indexModels(md.models);
    namingSaved = (nm && nm.chain) || [];
    S.namingChain = [...namingSaved];
    namingRender(catalog);
    $("#models-path").innerHTML = `${M_ICONS.file}<span>${esc(md.path)}</span>`;
    const box = $("#providers-list");
    if (!pv.providers.length) {
      box.innerHTML = I18n.html`<div class="mempty">${M_ICONS.hub}
        <div class="t">还没有供应商</div>
        <div class="s">添加供应商后可获取模型列表，也可手动添加模型 ID。</div>
        <button class="btn primary" data-pact="add">+ 添加供应商</button></div>`;
      return;
    }
    box.innerHTML = pv.providers.map(p => {
      const mine = catalog.byProvider.get(p.name) || [];
      return I18n.html`<div class="pblock">
        <div class="pb-head">
          <div class="pb-id">
            <div class="pb-r1">${brandIcon(p.name)}${keyLed(p)}<span class="pb-name">${esc(p.name)}</span>
              <span class="tag blue">${PROVIDER_CN[p.provider] || esc(p.provider)}</span>
              <span class="pb-count">${mine.length} 个模型</span></div>
            <div class="pb-url" title="${esc(p.base_url || "")}">${esc(p.base_url || I18n.t("（未填 API 地址）"))}</div>
          </div>
          <div class="pb-ops">
            ${ProviderAccounts.controls(p)}
            <button class="btn sm tinted" data-pact="fetch" data-name="${esc(p.name)}" style="display:inline-flex;align-items:center;gap:6px">${M_ICONS.sync}获取模型列表</button>
            <button class="icon-btn" title="编辑供应商" data-pact="edit" data-name="${esc(p.name)}">${M_ICONS.pencil}</button>
            <button class="icon-btn danger" title="删除供应商" data-pact="del" data-name="${esc(p.name)}">${M_ICONS.trash}</button>
          </div>
        </div>
        <div class="mgrid model-list">${mine.map(m => modelTile(m, catalog)).join("")}
          ${mine.length ? "" : `<div class="model-empty-quota">${ProviderAccounts.quotaSlot(p.name)}</div>`}
          <button class="ghost-tile" data-pact="addto" data-name="${esc(p.name)}">${M_ICONS.plus}手动添加模型</button>
        </div>
      </div>`;
    }).join("");
    ProviderAccounts.reset();
    ProviderAccounts.refreshVisible();
  } catch (e) { toast(e.message, true); }
}

/* ================= 会话自动命名候选链 =================
   顺序即降级顺序，完全由用户排定：点击候选池里的模型，按点击先后入链；
   前面的调不动，命名调用自动换下一个；留空 = 跟随编队页灵枢指派。
   保存后即刻生效（每次命名调用热读，无需重启）。 */
let namingSaved = [];
function namingRender(catalog = indexModels(S.models?.models || [])) {
  const chain = S.namingChain || [];
  const registry = (S.models && S.models.models) || [];
  const selected = new Set(chain);
  const provOf = id => catalog.byKey.get(id)?.provider_ref || I18n.t("未知供应商");
  $("#naming-chain").innerHTML = chain.map((m, i) => I18n.html`<li>
      <span class="nb-idx">${i + 1}</span><span class="nb-prov" title="供应商">${esc(provOf(m))}</span><b>${esc(catalog.byKey.get(m)?.model || m)}</b>
      <span class="nb-ops-row">
        <button type="button" data-nm="up" data-i="${i}" ${i === 0 ? "disabled" : ""} title="前移">↑</button>
        <button type="button" data-nm="down" data-i="${i}" ${i === chain.length - 1 ? "disabled" : ""} title="后移">↓</button>
        <button type="button" class="danger" data-nm="del" data-i="${i}" title="移出候选链">✕</button>
      </span></li>`).join("")
    || I18n.html`<li class="nb-empty">未配置，使用灵枢的模型。</li>`;
  // 候选池：注册表全量模型（带供应商标识），点击即按先后顺序入链
  $("#naming-pool").innerHTML = registry.map(m => {
    const on = selected.has(modelKey(m));
    return `<button type="button" class="nb-chip${on ? " on" : ""}" data-nm="add"
              data-model="${esc(modelKey(m))}"${on ? " disabled" : ""}
              title="${on ? I18n.t("已在候选链上（可在上方列表移除）") : I18n.t("点击加入候选链，按点击顺序排列")}">
              <span class="nb-prov">${esc(m.provider_ref || I18n.t("未知供应商"))}</span><b>${esc(m.model)}</b></button>`;
  }).join("") || I18n.html`<span class="nb-pool-empty">注册表中还没有模型——先在下方添加。</span>`;
  const dirty = JSON.stringify(chain) !== JSON.stringify(namingSaved);
  $("#naming-bar").hidden = !dirty;
  $("#naming-save").disabled = !dirty;
}
$("#naming-chain").addEventListener("click", ev => {
  const b = ev.target.closest("[data-nm]"); if (!b) return;
  const i = +b.dataset.i, chain = S.namingChain, act = b.dataset.nm;
  if (act === "up" && i > 0) [chain[i - 1], chain[i]] = [chain[i], chain[i - 1]];
  else if (act === "down" && i < chain.length - 1) [chain[i], chain[i + 1]] = [chain[i + 1], chain[i]];
  else if (act === "del") chain.splice(i, 1);
  else return;
  namingRender();
});
$("#naming-pool").addEventListener("click", ev => {
  const b = ev.target.closest("[data-nm=add]"); if (!b || b.disabled) return;
  S.namingChain.push(b.dataset.model);
  namingRender();
});
$("#naming-revert").onclick = () => { S.namingChain = [...namingSaved]; namingRender(); };
$("#naming-save").onclick = async () => {
  const btn = $("#naming-save");
  btn.disabled = true;
  try {
    const d = await api("/api/models/naming", { method: "PUT", body: JSON.stringify({ models: S.namingChain }) });
    namingSaved = d.chain || [];
    S.namingChain = [...namingSaved];
    namingRender();
    toast(I18n.t("命名候选链已保存，即刻生效"));
  } catch (e) { toast(e.message, true); btn.disabled = false; }
};

window.modelTest = async model => {
  toast(I18n.t`正在测试 ${modelLabel(model)} …`);
  try {
    const d = await api("/api/models/test", { method: "POST", body: JSON.stringify({ model }) });
    if (d.ok) toast(d.note || I18n.t`连通正常 · ${d.latency_ms}ms${d.sample ? " · " + d.sample : ""}`);
    else toast(d.error || I18n.t("测试失败"), true);
  } catch (e) { toast(e.message, true); }
};

/* 模型页操作走事件委托（容器级单监听，随 innerHTML 重渲不受影响）：
   按钮只带 data-*，点击时从 dataset 取回原值再调对应动作 */
$("#providers-list").addEventListener("click", ev => {
  const mb = ev.target.closest("[data-mact]");
  if (mb) {
    const fn = { test: modelTest, edit: modelEdit, del: modelDel }[mb.dataset.mact];
    if (fn) fn(mb.dataset.model);
    return;
  }
  const pb = ev.target.closest("[data-pact]");
  if (pb) {
    const fn = { add: providerAdd, fetch: providerFetch, edit: providerEdit,
                 del: providerDel, addto: modelAddTo, quota: ProviderAccounts.refresh,
                 login: ProviderAccounts.login, logout: ProviderAccounts.logout }[pb.dataset.pact];
    if (fn) fn(pb.dataset.name);
  }
});

function fillPrefs(sel) {
  $("#m-pref").innerHTML = (S.providers || []).map(p =>
    `<option value="${esc(p.name)}" ${p.name === sel ? "selected" : ""}>${esc(p.name)}</option>`).join("");
}

/* 思考强度多选：常用档仅作快捷便利（可再自定义任意取值），
   保存的就是模型自己声明的取值列表，灵枢仍只从其中挑选 */
const EFFORT_COMMON = ["minimal", "low", "medium", "high", "xhigh", "ultra", "max"];

function renderEffChips() {
  const common = EFFORT_COMMON.map(v =>
    `<button type="button" class="eff-chip ${S.effSel.includes(v) ? "on" : ""}"
      data-eff="${esc(v)}">${esc(v)}</button>`);
  const custom = S.effSel.filter(v => !EFFORT_COMMON.includes(v)).map(v =>
    I18n.html`<button type="button" class="eff-chip custom on" data-eff="${esc(v)}"
      title="点击移除">${esc(v)}</button>`);
  $("#m-eff-chips").innerHTML = common.join("") + custom.join("");
  $("#m-eff-chips").querySelectorAll(".eff-chip").forEach(chip => chip.onclick = () => {
    const v = chip.dataset.eff;
    S.effSel = S.effSel.includes(v) ? S.effSel.filter(x => x !== v) : [...S.effSel, v];
    renderEffChips();
  });
}

function addEffCustom() {
  const v = $("#m-eff-custom").value.trim();
  if (!v) return;
  if (!S.effSel.includes(v)) S.effSel = [...S.effSel, v];
  $("#m-eff-custom").value = "";
  renderEffChips();
}
$("#m-eff-add").onclick = addEffCustom;
$("#m-eff-custom").addEventListener("keydown", e => {
  if (e.key === "Enter") { e.preventDefault(); addEffCustom(); }
});

window.modelEdit = model => {
  const m = (S.models.models || []).find(x => modelKey(x) === model);
  if (!m) return;
  S.medit = model;
  $("#m-dlg-title").textContent = I18n.t`编辑模型 ${m.model} · ${m.provider_ref || I18n.t("默认")}`;
  $("#m-model").value = m.model || "";
  fillPrefs(m.provider_ref || "");
  $("#m-ctx").value = m.context_window || 0;
  $("#m-cache").value = m.prompt_caching || "provider_default";
  selxSync($("#m-cache"));
  S.effSel = [...(m.reasoning_effort_levels || [])];
  renderEffChips();
  $("#m-caps-box").querySelectorAll("input").forEach(c =>
    c.checked = (m.capabilities || []).includes(c.value));
  $("#m-dlg").showModal();
};

window.modelAddTo = ref => {
  S.medit = null;
  $("#m-dlg-title").textContent = I18n.t("手动添加模型");
  $("#m-model").value = "";
  $("#m-caps-box").querySelectorAll("input").forEach(c => c.checked = false);
  S.effSel = []; $("#m-eff-custom").value = "";
  renderEffChips();
  fillPrefs(ref);
  $("#m-ctx").value = 0;
  $("#m-cache").value = "provider_default"; selxSync($("#m-cache"));
  $("#m-dlg").showModal();
};

window.modelDel = async model => {
  try { await api(`/api/models/${encodeURIComponent(model)}`, { method: "DELETE" });
    toast(I18n.t`已删除 ${modelLabel(model)}`); loadModels(); }
  catch (e) { toast(e.message, true); }
};

$("#m-cancel").onclick = () => $("#m-dlg").close();
$("#m-save").onclick = async () => {
  const existing = (S.models?.models || []).find(x => modelKey(x) === S.medit) || {};
  const body = {
    ...Object.fromEntries(Object.entries(existing).filter(([,value]) => value != null)),
    model: $("#m-model").value.trim(), provider_ref: $("#m-pref").value,
    context_window: +$("#m-ctx").value || 0,
    reasoning_effort_levels: [...S.effSel],
    capabilities: [...$("#m-caps-box").querySelectorAll("input:checked")]
      .map(c => c.value),
    prompt_caching: $("#m-cache").value,
  };
  if (!body.model) { toast(I18n.t("模型 ID 必填"), true); return; }
  try {
    if (S.medit) await api(`/api/models/${encodeURIComponent(S.medit)}`,
      { method: "PUT", body: JSON.stringify(body) });
    else await api("/api/models", { method: "POST", body: JSON.stringify(body) });
    $("#m-dlg").close(); toast(I18n.t("已保存，新会话即刻生效")); loadModels();
  } catch (e) { toast(e.message, true); }
};

/* ---------- 供应商 ---------- */
window.providerEdit = name => {
  const p = (S.providers || []).find(x => x.name === name);
  if (!p) return;
  S.pedit = name;
  $("#p-dlg-title").textContent = I18n.t`编辑供应商 ${name}`;
  $("#p-name").value = p.name;
  $("#p-type").value = ["anthropic_compatible", "ollama"].includes(p.provider) ? p.provider : "openai_compatible";
  $("#p-baseurl").value = p.base_url || "";
  $("#p-keyval").value = "";
  ProviderAccounts.fill(p);
  $("#p-keystat").textContent =
    p.key_source === "vault" ? I18n.t("当前密钥：已保存（留空表示保持不变；重填即覆盖）")
    : I18n.t("当前密钥：未配置");
  $("#p-search").value = "";
  $("#p-dlg").showModal();
  loadPresets().then(() => renderPanel((S.presets || []).find(x =>
    x.base_url?.replace(/\/$/, "") === p.base_url?.replace(/\/$/, "")
    && (x.billing_scheme || "manual") === (p.billing_scheme || "manual"))?.key || "custom"));
};

window.providerDel = async name => {
  try { await api(`/api/providers/${encodeURIComponent(name)}`, { method: "DELETE" });
    toast(I18n.t`已删除供应商 ${name}`); loadModels(); }
  catch (e) { toast(e.message, true); }
};

async function loadPresets() {
  if (S.presets) return S.presets;
  try { S.presets = (await api("/api/provider-presets")).presets || []; }
  catch { S.presets = []; }
  return S.presets;
}

/* 厂商徽章：品牌色 + 短标识字符（预设目录自带 brand/mark）；
   自选名按名称包含匹配预设，匹配不到取首字符 + 中性灰 */
function brandOf(name) {
  const n = String(name || "").toLowerCase();
  const hit = (S.presets || []).find(x => n === x.key)
           || (S.presets || []).find(x => n.includes(x.key));
  if (hit) return hit;
  return { mark: [...String(name || "?").trim()][0]?.toUpperCase() || "?",
           brand: "var(--ink-3)" };
}

function brandIcon(name, cls = "") {
  const b = brandOf(name);
  const mono = (b.mark || "").length > 1 ? ' style="font-family:var(--mono);font-size:.86em"' : "";
  return `<span class="brand-ic ${cls}" style="background:${esc(b.brand)}"${mono}>${esc(b.mark)}</span>`;
}

function providerAdded(p) {
  return (S.providers || []).some(x => {
    return x.base_url?.replace(/\/$/, "") === p.base_url?.replace(/\/$/, "")
      && (x.billing_scheme || "manual") === (p.billing_scheme || "manual");
  });
}

/* 供应商面板左栏：厂商列表（预设 + 自定义）；点击带入右侧表单 */
const presetFolders = new Set();
function renderPanel(selKey) {
  const list = $("#p-presets");
  const items = [{ key: "custom", name: I18n.t("自定义") }, ...(S.presets || [])];
  const itemHTML = (p, child = false) => {
    const ic = p.key === "custom"
      ? `<span class="brand-ic sm" style="background:transparent;border:1px dashed var(--ink-3);color:var(--ink-3);box-shadow:none">+</span>`
      : brandIcon(p.key, "sm");
    const added = p.key !== "custom" && providerAdded(p) ? I18n.html`<span class="pp-added">已添加</span>` : "";
    return `<button type="button" class="pp-item ${child ? "pp-child" : ""} ${p.key === selKey ? "on" : ""}"
      data-pkey="${esc(p.key)}" aria-pressed="${p.key === selKey}" data-text="${esc([p.name, p.key, p.family_name, p.edition, p.base_url].join(" ").toLowerCase())}">
      ${child ? '<span class="pp-branch" aria-hidden="true">└</span>' : ic}<b>${esc(child ? p.edition || p.name : p.family_name || p.name)}</b>${added}</button>`;
  };
  const groups = new Map();
  for (const p of items.slice(1)) {
    const family = p.family || p.key;
    if (!groups.has(family)) groups.set(family, []);
    groups.get(family).push(p);
  }
  list.innerHTML = itemHTML(items[0]) + [...groups.entries()].map(([family, variants]) => {
    if (variants.length === 1) return itemHTML(variants[0]);
    const selected = variants.some(p => p.key === selKey);
    if (selected) presetFolders.add(family);
    return `<details class="pp-family" data-family="${esc(family)}" ${presetFolders.has(family) ? "open" : ""}>
      <summary>${brandIcon(variants[0].key, "sm")}<b>${esc(variants[0].family_name || variants[0].name)}</b><span class="pp-version-count">${variants.length}</span><span class="pp-chevron" aria-hidden="true">›</span></summary>
      <div class="pp-children">${variants.map(p => itemHTML(p, true)).join("")}</div></details>`;
  }).join("");
  list.querySelectorAll(".pp-family").forEach(folder => folder.addEventListener("toggle", () => {
    if ($("#p-search").value.trim()) return;
    if (folder.open) presetFolders.add(folder.dataset.family); else presetFolders.delete(folder.dataset.family);
  }));
  filterPresets();
  list.querySelectorAll(".pp-item").forEach(btn => btn.onclick = () => {
    const p = items.find(x => x.key === btn.dataset.pkey);
    if (!p) return;
    renderPanel(p.key);
    if (p.key === "custom") {
      $("#p-name").value = ""; $("#p-baseurl").value = "";
      $("#p-type").value = "openai_compatible";
    } else {
      $("#p-name").value = p.key;
      $("#p-type").value = ["anthropic_compatible", "ollama"].includes(p.provider) ? p.provider : "openai_compatible";
      $("#p-baseurl").value = p.base_url || "";
    }
    ProviderAccounts.fill(p.key === "custom" ? {} : p);
  });
}

function filterPresets() {
  const q = $("#p-search").value.trim().toLowerCase();
  $("#p-presets").querySelectorAll(".pp-item").forEach(el => {
    el.hidden = !!q && !el.dataset.text.includes(q);
  });
  $("#p-presets").querySelectorAll(".pp-family").forEach(folder => {
    folder.hidden = ![...folder.querySelectorAll(".pp-item")].some(el => !el.hidden);
    folder.open = q ? !folder.hidden : presetFolders.has(folder.dataset.family);
  });
}
$("#p-search").addEventListener("input", filterPresets);

window.providerAdd = async () => {
  S.pedit = null;
  $("#p-dlg-title").textContent = I18n.t("添加供应商");
  for (const id of ["p-name", "p-baseurl", "p-keyval"]) $("#" + id).value = "";
  $("#p-keystat").textContent = "";
  $("#p-type").value = "openai_compatible";
  $("#p-search").value = "";
  ProviderAccounts.fill({});
  $("#p-dlg").showModal();
  await loadPresets();
  renderPanel("custom");
};
$("#p-add").onclick = window.providerAdd;
$("#p-cancel").onclick = () => $("#p-dlg").close();
$("#p-save").onclick = async () => {
  const name = $("#p-name").value.trim();
  const keyval = $("#p-keyval").value.trim();
  if (!name) { toast(I18n.t("供应商名必填"), true); return; }
  const original = (S.providers || []).find(p => p.name === S.pedit) || {};
  const body = { api_format: original.api_format || "auto", name, provider: $("#p-type").value, base_url: $("#p-baseurl").value.trim(),
                 api_key: "", ...ProviderAccounts.fields() };
  // 密钥只走直填：服务端加密存入密钥库；编辑时留空 = 库内密钥保持不变
  if (keyval) body.api_key_value = keyval;
  try {
    if (S.pedit) await api(`/api/providers/${encodeURIComponent(S.pedit)}`,
      { method: "PUT", body: JSON.stringify(body) });
    else await api("/api/providers", { method: "POST", body: JSON.stringify(body) });
    $("#p-dlg").close(); toast(I18n.t("供应商已保存")); loadModels();
  } catch (e) { toast(e.message, true); }
};

/* ---------- 拉取云端模型列表 ---------- */
window.providerFetch = async name => {
  S.fprov = name;
  $("#fm-body").innerHTML = I18n.html`<span class="muted">正在从「${esc(name)}」拉取可用模型…</span>`;
  $("#fm-add").disabled = true;
  $("#fm-dlg").showModal();
  try {
    const d = await api(`/api/providers/${encodeURIComponent(name)}/models`,
      { method: "POST", body: "{}" });
    if (!d.ok) {
      $("#fm-body").innerHTML = I18n.html`<div class="empty-note" style="padding:14px 0;text-align:left">
        未能自动获取模型列表：${esc(d.error || I18n.t("未知错误"))}
        ${(d.tried || []).length ? I18n.html`<br><span class="muted" style="font-size:11.5px">已尝试：${esc((d.tried || []).join("　"))}</span>` : ""}
        <br>关掉本窗口，用「手动添加模型」直接填模型 ID 即可。</div>`;
      return;
    }
    const known = new Set((S.models.models || []).filter(x => x.provider_ref === name).map(x => x.model));
    $("#fm-body").innerHTML = I18n.html`
      <div class="muted" style="font-size:12px;margin-bottom:9px">来自 ${esc(d.url)}，共 ${d.models.length} 个可用模型。
        勾选后批量启用，之后可在模型行里逐个调整画像。</div>
      <div class="fm-list">${d.models.map(x => {
        const on = known.has(x.id);
        return `<label class="fm-row"><input type="checkbox" value="${esc(x.id)}" ${on ? "disabled" : "checked"}>
          <span class="mono" style="font-size:12.5px">${esc(x.id)}</span>
          ${x.display_name && x.display_name !== x.id ? `<span class="muted" style="font-size:11.5px">${esc(x.display_name)}</span>` : ""}
          ${on ? I18n.html`<span class="tag green" style="margin-left:auto">已启用</span>` : ""}</label>`;
      }).join("")}</div>
      <label style="display:flex;gap:9px;align-items:center;margin-top:11px;font-size:12px;font-weight:600;color:var(--ink-2)"></label>`;
    const boxes = [...$("#fm-body").querySelectorAll("input[type=checkbox]:not(:disabled)")];
    const sync = () => { $("#fm-add").disabled = !boxes.some(b => b.checked); };
    boxes.forEach(b => b.onchange = sync); sync();
  } catch (e) {
    $("#fm-body").innerHTML = `<div class="empty-note" style="padding:14px 0">${esc(e.message)}</div>`;
  }
};
$("#fm-cancel").onclick = () => $("#fm-dlg").close();
$("#fm-add").onclick = async () => {
  const ids = [...$("#fm-body").querySelectorAll("input[type=checkbox]")]
    .filter(b => b.checked && !b.disabled).map(b => b.value);
  if (!ids.length) return;
  try {
    const d = await api("/api/models/batch", { method: "POST",
      body: JSON.stringify({ provider_ref: S.fprov, models: ids }) });
    $("#fm-dlg").close();
    toast(I18n.t`已启用 ${d.added.length} 个模型${d.added.length < ids.length ? I18n.t`（${ids.length - d.added.length} 个已存在，跳过）` : ""}`);
    loadModels();
  } catch (e) { toast(e.message, true); }
};
