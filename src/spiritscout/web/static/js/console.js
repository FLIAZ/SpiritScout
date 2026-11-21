/* ================= 会话输入坞：意见 + 附件（页底常驻） ================= */
const compInput = $("#comp-input");
compInput.addEventListener("input", () => {
  syncComposerControl();
  autogrow(compInput, 26, 140);
});
function compGrow() {
  compInput.style.height = "auto";
  compInput.style.height = Math.min(compInput.scrollHeight, 180) + "px";
}
/* 附件 chip（初始任务与会话输入坞共用）；files 数组由调用方持有 */
const composerImagePreviews = new Map();
const isComposerImage = file => !file.directory && (
  /^image\/(png|jpeg|gif|webp|avif|bmp)$/i.test(file.type || "") ||
  (!file.type && /\.(png|jpe?g|gif|webp|avif|bmp)$/i.test(file.name || "")));
function renderFileChips(sel, files) {
  const box = $(sel);
  const previews=composerImagePreviews.get(sel) || new Map();
  composerImagePreviews.set(sel,previews);
  for(const [file,url] of previews) if(!files.includes(file)) {
    URL.revokeObjectURL(url);previews.delete(file);
  }
  box.innerHTML = files.map((f, i) => {
    if(isComposerImage(f)) {
      if(!previews.has(f))previews.set(f,URL.createObjectURL(f));
      return I18n.html`<span class="comp-image" title="${esc(f.name)}">
        <button type="button" class="comp-image-open" data-preview-i="${i}" aria-label="放大查看 ${esc(f.name)}">
          <img src="${esc(previews.get(f))}" alt="${esc(f.name)}" decoding="async">
          <span class="comp-image-fallback" hidden>无法预览</span>
        </button>
        <button type="button" class="comp-image-remove" data-i="${i}" title="移除 ${esc(f.name)}" aria-label="移除 ${esc(f.name)}"><svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="m4 4 8 8M12 4l-8 8"/></svg></button>
      </span>`;
    }
    return I18n.html`
    <span class="comp-file"><b>${esc(f.name)}</b><i>${fmtSize(f.size)}</i>
      <button type="button" data-i="${i}" title="移除">✕</button></span>`;
  }).join("");
  box.querySelectorAll("button[data-i]").forEach(b =>
    b.onclick = () => { files.splice(+b.dataset.i, 1); renderFileChips(sel, files); });
  box.querySelectorAll("button[data-preview-i]").forEach(button=>{
    button.onclick=()=>FileCards.openLocalImage(files[+button.dataset.previewI]);
  });
  box.querySelectorAll(".comp-image img").forEach(image=>{
    image.onerror=()=>{image.hidden=true;image.nextElementSibling.hidden=false;};
  });
  if(sel === "#comp-files") syncComposerControl();
}
/* 注意传 getter：S.compFiles / S.homeFiles 在切会话、切视图、发送后都会被整体
 * 换成新数组，若在绑定时不按引用传入，onchange 会把文件推进已废弃的旧数组，
 * 芯片照常显示但 compSend 读到空列表——附件静默丢失 */
function bindAttach(btnId, inputId, getFiles, chipsSel) {
  $(btnId).onclick = () => $(inputId).click();
  $(inputId).onchange = () => {
    const files = getFiles();
    for (const f of $(inputId).files) files.push(f);
    $(inputId).value = "";
    renderFileChips(chipsSel, files);
  };
}
bindAttach("#comp-attach", "#comp-file", () => S.compFiles, "#comp-files");
bindAttach("#home-attach", "#home-file", () => S.homeFiles, "#home-files");
compInput.addEventListener("input", compGrow);
compInput.addEventListener("keydown", ev => {
  if (typeof ComposerCommands !== "undefined" && ComposerCommands.keydown(ev, "session")) return;
  if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing && ev.keyCode !== 229) {
    ev.preventDefault();
    compSend();
  }
});
$("#comp-send").onclick = () => {
  syncComposerControl();
  if ($("#comp-send").disabled) return;
  return $("#comp-send").dataset.action === "send" ? compSend() : toggleTaskPause();
};

async function uploadAttachment(file, sessionId = S.cur) {
  const r = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/attachments?name=${encodeURIComponent(file.name)}`,
    { method: "POST", body: file });
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
}
const composerDeliveries = new Map();
async function compSend() {
  if (typeof ComposerDrop !== "undefined" && ComposerDrop.pending("session")) {
    toast(I18n.t("正在读取附件，请稍后发送"), true); return;
  }
  const sessionId = S.cur, text = compInput.value.trim(), files = [...S.compFiles];
  if ((!text && !files.length) || sessionMessagePending.has(sessionId) ||
      (typeof ComposerModels !== "undefined" && ComposerModels.pending())) return;
  const sess = sessionId ? S.sessions.find(x => x.session_id === sessionId) : null;
  if (!sess) { toast(I18n.t("先选择会话或下达任务"), true); return; }
  if (typeof ComposerCommands !== "undefined" && ComposerCommands.parse(text) &&
      await ComposerCommands.execute(text, files)) return;
  let delivery = composerDeliveries.get(sessionId);
  if (!delivery || delivery.text !== text || delivery.files.length !== files.length ||
      delivery.files.some((file, index) => file !== files[index])) {
    const unique = globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
    delivery = {text, files, messageId: `message-${unique}`, uploadIds: null};
    composerDeliveries.set(sessionId, delivery);
  }
  sessionMessagePending.add(sessionId);
  syncComposerControl();
  try {
    if (!delivery.uploadIds) delivery.uploadIds = await stageFiles(files);
    const resp = await api(`/api/sessions/${encodeURIComponent(sessionId)}/messages`,
      {method:"POST", body:JSON.stringify({content:typeof ComposerCommands !== "undefined" ? ComposerCommands.messageText(text) : text, upload_ids:delivery.uploadIds,
        message_id:delivery.messageId})});
    composerDeliveries.delete(sessionId);
    const current = S.state?.session?.session_id === sessionId ? S.state.session : sess;
    const waiting = resp.waiting_for_slot ?? (current.status === "running" && current.phase === "queued");
    toast(waiting ? I18n.t("消息已保存，会话仍在等待调度。") : I18n.t("消息已保存，等待灵枢处理。"));
    if (S.cur === sessionId && resp.resumed) {
      startStreams(false); refreshState(); refreshSessions();
    }
    if (S.cur === sessionId && compInput.value.trim() === text &&
        S.compFiles.length === files.length && S.compFiles.every((file, index) => file === files[index])) {
      compInput.value = ""; compGrow(); S.compFiles = [];
      renderFileChips("#comp-files", S.compFiles);
    }
  } catch (e) { toast(e.message, true); }
  finally { sessionMessagePending.delete(sessionId); syncComposerControl(); }
}

/* ================= 发起会话 ================= */
const autogrow = (el, min, max) => {
  el.style.height = "auto";
  el.style.height = Math.min(max, Math.max(min, el.scrollHeight)) + "px";
};
const ctfChallengeFields = {name: "#c-ctf-name", description: "#c-ctf-description", hint: "#c-ctf-hint", address: "#c-ctf-address"};
function ctfChallengeDraft() {
  if ($("#c-mode").value !== "ctf") return null;
  return Object.fromEntries(Object.entries(ctfChallengeFields).map(([key, selector]) => [key, $(selector).value.trim()]));
}
function ctfChallengeText(draft) {
  if (!draft) return "";
  return [["name", I18n.t("题目名称")], ["description", I18n.t("题目描述")],
    ["hint", I18n.t("题目提示")], ["address", I18n.t("题目地址")]]
    .filter(([key]) => draft[key]).map(([key, label]) => `${label}：\n${draft[key]}`).join("\n\n");
}
const homeSync = () => {
  $("#c-go").classList.toggle("ready", (!!$("#c-mission").value.trim() || !!ctfChallengeText(ctfChallengeDraft())) && !!$("#c-mode").value);
  autogrow($("#c-mission"), $("#c-mode").value === "ctf" ? 100 : 160, 360);
};
const missionModeHints = {
  src: I18n.t("评估已授权的网站或资产，验证漏洞并生成报告。"),
  ctf: I18n.t("解答题目入口或附件，获取答案并生成解题报告。"),
  awdp: I18n.t("上传有漏洞的服务源码或二进制，灵枢定位漏洞、最小修复并按原始目录结构打包补丁。"),
  competition: I18n.t("粘贴赛方规则、目标与平台说明，灵枢按实际规则规划，支持自定义赛制。"),
};
function ctfTypeHintPayload(mode = $("#c-mode").value) {
  if (mode !== "ctf") return {};
  const types = [...document.querySelectorAll('input[name="ctf-type"]:checked')].map(input => input.value);
  const note = $("#c-ctf-type-note").value.trim();
  return types.length || note ? {ctf_type_hints: types, ctf_type_note: note} : {};
}
$("#c-mode").onchange = () => {
  if (typeof ProxyPoolPage !== 'undefined') ProxyPoolPage.loadSelector($('#c-mode').value);
  $("#c-ctf-challenge").hidden = $("#c-mode").value !== "ctf";
  $("#c-src-strategy").hidden = $("#c-mode").value !== "src";
  $("#c-generate-wp-option").hidden = $("#c-mode").value !== "competition";
  $("#c-mission").placeholder = $("#c-mode").value === "ctf" ? I18n.t("在此输入额外要求、已有思路或需要 Agent 注意的事项（选填）…") : I18n.t("输入目标地址、授权范围、题目描述或测试要求…");
  $("#c-ctf-types").hidden = $("#c-mode").value !== "ctf";
  $("#c-multiple-answers-option").hidden = $("#c-mode").value !== "ctf";
  $("#c-mode-hint").textContent = missionModeHints[$("#c-mode").value] || I18n.t("先选择任务类型，再填写目标与要求。");
  document.querySelectorAll('input[name="home-mode"]').forEach(input => {
    input.checked = input.value === $("#c-mode").value;
  });
  selxSync($("#c-mode"));
  homeSync();
};
document.querySelectorAll('input[name="home-mode"]').forEach(input => {
  input.addEventListener("change", () => {
    $("#c-mode").value = input.value;
    $("#c-mode").onchange();
  });
});
$("#c-proxy-manage")?.addEventListener("click", () => show("proxy"));
$("#c-mission").addEventListener("input", homeSync);
Object.values(ctfChallengeFields).forEach(selector => $(selector).addEventListener("input", homeSync));
homeSync();
document.querySelectorAll(".chip-s").forEach(b => b.onclick = () => {
  if (b.dataset.mode) { $("#c-mode").value = b.dataset.mode; $("#c-mode").onchange(); }
  const t = b.dataset.mission || b.textContent.trim();
  $("#c-mission").value = t.includes("example.com")
    ? t : `${t}：`;
  homeSync();
  $("#c-mission").focus();
});
$("#c-go").onclick = async () => {
  const mission = $("#c-mission").value.trim();
  if (typeof ComposerCommands !== "undefined" && ComposerCommands.parse(mission) &&
      await ComposerCommands.execute(mission, [...S.homeFiles], "home")) return;
  const challengeDraft = ctfChallengeDraft();
  const challengeText = ctfChallengeText(challengeDraft);
  if (!mission && !challengeText) { toast(I18n.t("先用自然语言描述任务——目标与要求写清楚即可"), true); return; }
  const extra = typeof ComposerCommands !== "undefined" ? ComposerCommands.messageText(mission) : mission;
  await launchMission({ mission:[challengeText, extra].filter(Boolean).join("\n\n"),
    ...(challengeDraft?.name ? {title:challengeDraft.name} : {}),
    multiple_answers: $("#c-mode").value === "ctf" && $("#c-multiple-answers").checked }, [...S.homeFiles], challengeDraft);
};
$("#c-mission").addEventListener("keydown", ev => {
  if (typeof ComposerCommands !== "undefined" && ComposerCommands.keydown(ev, "home")) return;
  if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing && ev.keyCode !== 229) {
    ev.preventDefault();
    $("#c-go").click();
  }
});

/* 附件暂存：先传到暂存区拿 upload_id，随创建请求一并材料化进会话工作区 */
async function stageFiles(files) {
  const ids = [];
  for (const f of files) {
    if (f.directory) {
      const staged = await api(`/api/uploads/folders?name=${encodeURIComponent(f.name)}`, {method:"POST"});
      for (const entry of f.entries) {
        const url = `/api/uploads/folders/${encodeURIComponent(staged.upload_id)}?path=${encodeURIComponent(entry.path)}` +
          (entry.directory ? "&directory=true" : "");
        const response = await fetch(url, {method:"PUT", body:entry.file || new Blob()});
        if (!response.ok) {
          let detail = response.statusText;
          try { detail = (await response.json()).detail || detail; } catch {}
          throw new Error(detail);
        }
      }
      ids.push(staged.upload_id);
      continue;
    }
    const r = await fetch(`/api/uploads?name=${encodeURIComponent(f.name)}`,
      { method: "POST", body: f });
    if (!r.ok) {
      let msg = r.statusText;
      try { msg = (await r.json()).detail || msg; } catch {}
      throw new Error(msg);
    }
    ids.push((await r.json()).upload_id);
  }
  return ids;
}

async function launchMission(payload, files = [], challengeDraft = null) {
  if (typeof ComposerDrop !== "undefined" && ComposerDrop.pending("home")) {
    toast(I18n.t("正在读取附件，请稍后下达任务"), true); return;
  }
  if (!payload.mode) {
    const mode = $("#c-mode").value;
    if (!mode) {
      toast(I18n.t("请先选择任务类型"), true);
      (document.querySelector('input[name="home-mode"]') || $("#c-mode")).focus();
      return;
    }
    payload = { ...payload, mode };
  }
  if (payload.mode === "competition" && payload.generate_wp === undefined) {
    payload.generate_wp = $("#c-generate-wp").checked;
  }
  if (payload.mode === "ctf") payload = {...ctfTypeHintPayload("ctf"), ...payload};
  if (payload.mode === "src" && payload.asset_strategy === undefined) {
    payload.asset_strategy = [...document.querySelectorAll('input[name="src-strategy"]')]
      .find(input => input.checked)?.value || "edge_first";
  }
  if (payload.mode === 'src' && payload.proxy_plan_id === undefined) payload.proxy_plan_id = $('#c-proxy-plan')?.value || 'direct';
  if (payload.mode === "src" && window.__srcAssetSelection) {
    const selected = window.__srcAssetSelection.targets || [];
    if (selected.length && (!payload.project_id || payload.project_id === window.__srcAssetSelection.project_id)) payload = {...payload, asset_campaign_id: window.__srcAssetSelection.campaign_id, asset_values: selected, target: selected[0], scope: [...new Set([...(payload.scope || []), ...selected])], excluded_assets: [...new Set([...(payload.excluded_assets || []), ...(window.__srcAssetSelection.excluded || [])])]};
  }
  $("#c-go").disabled = true;
  try {
    if (!payload.project_id) {
      const project = $("#c-project")?.value || "";
      if (project) payload = { ...payload, project_id: project };
    }
    const selection = typeof ComposerModels !== "undefined" ? ComposerModels.selected("home") : "";
    if (selection && payload.model === undefined) payload = {...payload, model: selection};
    if (files.length) payload = { ...payload, upload_ids: await stageFiles(files) };
    const s = await api("/api/sessions", { method: "POST", body: JSON.stringify(payload) });
    toast(files.length ? I18n.t("任务已下达，附件已交给灵枢") : I18n.t("任务已下达，灵枢开始规划"));
    $("#c-mission").value = "";
    if (payload.asset_campaign_id === window.__srcAssetSelection?.campaign_id) window.__srcAssetSelection = null;
    syncAssetPickerVisibility();
    if (challengeDraft) {
      for (const [key, selector] of Object.entries(ctfChallengeFields)) {
        if ($(selector).value.trim() === challengeDraft[key]) $(selector).value = "";
      }
    }
    if (payload.mode === "competition" && $("#c-generate-wp").checked === payload.generate_wp) {
      $("#c-generate-wp").checked = false;
    }
    if (payload.mode === "ctf") {
      const current = ctfTypeHintPayload("ctf");
      if (JSON.stringify(current.ctf_type_hints || []) === JSON.stringify(payload.ctf_type_hints || []) &&
          (current.ctf_type_note || "") === (payload.ctf_type_note || "")) {
        document.querySelectorAll('input[name="ctf-type"]').forEach(input => { input.checked = false; });
        $("#c-ctf-type-note").value = "";
      }
    }
    if (files.length) { S.homeFiles = []; renderFileChips("#home-files", S.homeFiles); }
    homeSync();
    // 先进会话页：列表刷新是附带动作，不该挡在进场前面（服务器侧若正被
    // 长任务占住，await 列表会把进页一起压住）
    openSession(s.session_id);
    refreshSessions();
  } catch (e) {
    if (Array.isArray(e.detail) && e.detail.some(item => item.type === "extra_forbidden" &&
        ["ctf_type_hints", "ctf_type_note"].includes(item.loc?.at(-1)))) {
      toast(I18n.t("后端尚未加载题型提示功能。请在当前任务结束后重启服务；本次选项已保留。"), true);
    } else if (e.detail && e.detail.code === "rule_conflict") openRuleConflict(payload, e.detail);
    else toast(e.message, true);
  } finally { $("#c-go").disabled = false; }
}

function syncAssetPickerVisibility() {
  const button = $("#c-assets-pick"), project = $("#c-project"), mode = $("#c-mode");
  if (button) { button.hidden = mode?.value !== "src" || !project?.value; button.textContent = window.__srcAssetSelection ? I18n.t`已选 ${window.__srcAssetSelection.targets.length} 个资产（重新选择）` : I18n.t("选择已收集资产"); }
}

async function openAssetPicker(campaignId = "") {
  const project = $("#c-project")?.value;
  if (!project) return;
  try {
    const data = await api(`/api/projects/${encodeURIComponent(project)}/asset-campaigns`);
    if ($("#c-project")?.value !== project || $("#c-mode")?.value !== "src") return;
    const available = (data.campaigns || []).filter(c => ["completed", "completed_with_errors", "stopped", "failed"].includes(c.status) && Number(c.stats?.candidates));
    const dlg = document.createElement("dialog"); dlg.className = "project-dlg asset-picker-dialog";
    dlg.innerHTML = I18n.html`<h3>选择已收集资产</h3><p class="muted">先结束收集，再勾选本次测试目标。将仅附带 CSV 资产清单；SRC 按资产价值排序与测试，不重复全量收集。</p><label>收集批次<select data-batch><option value="">请选择批次</option>${available.map(c => `<option value="${esc(c.campaign_id)}">${esc((c.targets || []).join("、"))} · ${Number(c.stats?.candidates || 0)} 个资产 · ${esc(c.campaign_id)}</option>`).join("")}</select></label><div class="asset-picker-controls"><button class="btn sm" data-all>全选</button><button class="btn sm" data-none>清空选择</button><span data-count>未选择资产</span></div><p class="muted">默认勾选有 HTTP 响应或真实 DNS 解析的资产；待验证与单出口失败的候选仍保留，可按需勾选。创建行为确认本次选定范围，不自动带入关联资产。</p><div class="asset-picker-list" data-targets>请选择一个已结束的收集批次。</div><p class="pj-error" role="alert"></p><div class="dlg-ops"><button type="button" class="btn" data-cancel>取消</button><button type="button" class="btn primary" data-ok disabled>确认资产并带入会话</button></div>`;
    document.body.append(dlg);
    let revision = 0, batch = null, closed = false;
    const close = () => { closed = true; revision++; dlg.close(); dlg.remove(); };
    dlg.querySelector("[data-cancel]").onclick = close;
    dlg.addEventListener("cancel", e => { e.preventDefault(); close(); });
    const choices = () => [...dlg.querySelectorAll("[data-asset]:checked")];
    const sync = () => { const count = choices().length; dlg.querySelector("[data-count]").textContent = I18n.t`已选 ${count} 个资产`; dlg.querySelector("[data-ok]").disabled = !count; };
    dlg.querySelector("[data-all]").onclick = () => { dlg.querySelectorAll("[data-asset]").forEach(e => e.checked = true); sync(); };
    dlg.querySelector("[data-none]").onclick = () => { dlg.querySelectorAll("[data-asset]").forEach(e => e.checked = false); sync(); };
    const select = dlg.querySelector("[data-batch]");
    select.onchange = async () => {
      const stamp = ++revision; batch = null;
      dlg.querySelector("[data-ok]").disabled = true;
      dlg.querySelector("[data-count]").textContent = I18n.t("未选择资产");
      dlg.querySelector("[data-targets]").textContent = I18n.t("正在读取资产…");
      try {
        const result = select.value ? await api(`/api/asset-campaigns/${encodeURIComponent(select.value)}/targets`) : null;
        if (closed || stamp !== revision) return;
        batch = result;
        dlg.querySelector("[data-targets]").innerHTML = (result?.targets || []).map((item, i) => `<label><input type="checkbox" data-asset value="${i}" ${["http_responded", "http_verified", "dns_resolved"].includes(item.verification_status) ? "checked" : ""}><span><code>${esc(item.value)}</code>${item.title ? ` · ${esc(item.title)}` : ""}<small>${esc(item.verification_status || "待验证")}${item.http_status ? ` · HTTP ${esc(item.http_status)}` : ""} · ${esc(item.egress_name || "历史出口未知")}</small></span></label>`).join("") || I18n.html("批次暂无可选资产");
        dlg.querySelectorAll("[data-asset]").forEach(e => e.onchange = sync); sync();
      } catch (e) { if (!closed && stamp === revision) dlg.querySelector(".pj-error").textContent = e.message; }
    };
    dlg.querySelector("[data-ok]").onclick = () => {
      if (!batch || $("#c-project")?.value !== project || $("#c-mode")?.value !== "src") { close(); return; }
      const targets = choices().map(e => batch.targets[Number(e.value)].value);
      if (!targets.length) return;
      window.__srcAssetSelection = {targets, excluded: batch.excluded_assets || [], campaign_id: batch.campaign_id, project_id: project};
      if (!$("#c-mission").value.trim()) $("#c-mission").value = "根据已收集资产和我选择的优先策略排序，对本次选定目标开展 SRC 测试，保留可复现证据。";
      close(); homeSync(); syncAssetPickerVisibility(); toast(I18n.t`已选择 ${targets.length} 个资产，创建会话时仅附带 CSV`);
    };
    dlg.showModal();
    if (campaignId && available.some(c => c.campaign_id === campaignId)) { select.value = campaignId; await select.onchange(); }
  } catch (e) { toast(e.message, true); }
}

document.addEventListener("asset-collection:src", async e => {
  try {
    const {project_id, campaign_id} = e.detail;
    show("home");
    await ProjectsPage.loadCatalog();
    if (S.view !== "home") return;
    $("#c-project").value = project_id;
    $("#c-project").dispatchEvent(new Event("change"));
    const mode = document.querySelector('input[name="home-mode"][value="src"]');
    if (mode) { mode.checked = true; mode.dispatchEvent(new Event("change", {bubbles: true})); }
    $("#c-mode").value = "src"; $("#c-mode").dispatchEvent(new Event("change"));
    if (typeof selxSync === "function") selxSync($("#c-project"));
    await openAssetPicker(campaign_id);
  } catch (error) { toast(error.message, true); }
});

$("#c-assets-pick")?.addEventListener("click", () => openAssetPicker());
$("#c-project")?.addEventListener("change", () => { window.__srcAssetSelection = null; syncAssetPickerVisibility(); });
$("#c-mode")?.addEventListener("change", () => { if ($("#c-mode").value !== "src") window.__srcAssetSelection = null; syncAssetPickerVisibility(); });

/* 规则冲突即时确认：后端给出冲突清单，用户确认风险后带接受标记重发 */
let rcPayload = null;
function openRuleConflict(payload, detail) {
  rcPayload = payload;
  const items = Array.isArray(detail.conflicts) && detail.conflicts.length
    ? detail.conflicts : [detail.message || I18n.t("规则冲突")];
  $("#rc-list").innerHTML = items.map(c => `<li>${esc(c)}</li>`).join("");
  $("#rc-dlg").showModal();
}
$("#rc-cancel").onclick = () => $("#rc-dlg").close();
$("#rc-confirm").onclick = async () => {
  $("#rc-dlg").close();
  if (rcPayload) await launchMission({ ...rcPayload, accept_rule_conflicts: true });
};

/* ================= 启动 ================= */
(async function boot() {
  I18n.onSwitch(target => {
    target.searchParams.set("view", S.view);
    if (S.view === "session" && S.cur) {
      target.searchParams.set("session", S.cur);
      target.searchParams.set("tab", S.tab);
    }
  });
  // 轻量部署（Ollama 离线赛台）：只留 CTF/AWDP，隐藏 SRC/智能体入口
  if (window.__SPIRITSCOUT_LIGHT__) {
    document.querySelectorAll("[data-full-only]").forEach(el => { el.hidden = true; });
    document.querySelectorAll("[data-light-only]").forEach(el => { el.hidden = false; });
  }
  AgentSideChat.init();
  AgentWorkbench.init();
  // 先绑定交互；编队或项目目录的慢请求不应阻塞模型选择、通知与会话列表。
  ComposerModels.init();
  ConsoleNotify.init();
  const version = document.querySelector('meta[name="spiritscout-version"]')?.content;
  if (version) $("#ver").textContent = "v" + version;
  S.polling = ConsolePolling.create({refreshList: refreshSessions, refreshState,
    refreshNotify: () => ConsoleNotify.check(),
    isSession: () => S.view === "session" && !!S.cur,
    isRunning: () => S.view === "session" && !!S.cur && !TERMINAL.has(S.state?.session?.status),
    isVisible: () => document.visibilityState === "visible"});
  S.polling.start();
  api("/api/agents").then(data => { S.agents = data; }).catch(() => {});
  // 目录返回后 loadCatalog 会重新分组；无需让独立请求串行等待。
  ProjectsPage.loadCatalog().then(syncAssetPickerVisibility).catch(() => {});
  await refreshSessions();
  // 切换语言后回到原页面；URL 也适用于禁用本地存储的浏览器。
  const navigation = new URLSearchParams(typeof location === "undefined" ? "" : location.search);
  const returnView = navigation.get("view");
  if (navigation.has("lang") && returnView && Object.hasOwn(VIEWS, returnView)) {
    const target = new URL(location.href);
    for (const key of ["view", "session", "tab"]) target.searchParams.delete(key);
    history.replaceState(null, "", target);
    if (returnView === "session") {
      const session = navigation.get("session");
      if (S.sessions.some(item => item.session_id === session)) {
        openSession(session);
        const tab = navigation.get("tab");
        if (TABS.includes(tab)) document.querySelector(`[data-tab="${tab}"]`)?.click();
        return;
      }
    } else { show(returnView); return; }
  }
  const running = S.sessions.find(x => !TERMINAL.has(x.status));
  if (running && S.view === "home" && !S.cur) openSession(running.session_id);
})();
