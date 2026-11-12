/* ================= 视图：会话详情/拓扑/报告 ================= */
/* ================= 会话详情 ================= */
const TABS = ["chat", "topo", "assets", "challenges", "vulns", "flags", "experience", "traffic", "background", "events", "deliverables", "report"];
const STEERING_STATUS_CN = {delivered: I18n.t("已投递"), accepted: I18n.t("计划执行"), completed: I18n.t("已报告完成"), blocked: I18n.t("执行受阻")};
function openSession(id) {
  if (typeof ComposerCommands !== "undefined") ComposerCommands.reset();
  if (typeof AssetSecretViewer !== "undefined") AssetSecretViewer.close();
  if (S.cur !== id) {
    S.state = null;
    // 附件暂存是会话私有状态：切到另一会话即清空，上一会话的 chip
    // 不能泄漏到新会话（看起来像附件还在、会随下一条消息带走）
    S.compFiles = [];
    renderFileChips("#comp-files", S.compFiles);
    renderChallenges();
    $("#topo-ops-apv").replaceChildren();
    delete $("#topo-ops-apv").dataset.signature;
    $("#topo-ops-ask").replaceChildren();
    updateSessionActions();
  }
  if (typeof BackgroundTasks !== "undefined") BackgroundTasks.close();
  S.cur = id; S.tab = "chat"; S.evSeen = 0; S.topoView = null;
  SessionArchive.reset(id);
  ToolParameters.reset(id);
  if (typeof ConsoleSidebar !== "undefined") ConsoleSidebar.closeOnMobile();
  show("session");
  renderSessions();
  document.querySelectorAll("#s-tabs button").forEach(b => {
    b.classList.toggle("on", b.dataset.tab === "chat");
    b.setAttribute("aria-selected", String(b.dataset.tab === "chat"));
    b.tabIndex = b.dataset.tab === "chat" ? 0 : -1;
  });
  for (const t of TABS) $("#tab-" + t).hidden = t !== "chat";
  refreshState();
  startStreams();
}
document.querySelectorAll("#s-tabs button").forEach(b => {
  b.id = "session-tab-" + b.dataset.tab;
  b.setAttribute("role", "tab");
  b.setAttribute("aria-controls", "tab-" + b.dataset.tab);
  b.setAttribute("aria-selected", String(b.classList.contains("on")));
  b.tabIndex = b.classList.contains("on") ? 0 : -1;
  const panel = $("#tab-" + b.dataset.tab);
  panel.setAttribute("role", "tabpanel");
  panel.setAttribute("aria-labelledby", b.id);
  b.onclick = () => {
    S.tab = b.dataset.tab;
    if (typeof TrafficPage !== "undefined") TrafficPage.sessionTab();
    if (typeof CoveragePage !== "undefined") CoveragePage.navigate();
    if (typeof BackgroundTasks !== "undefined") { if (S.tab === "background") BackgroundTasks.open(); else BackgroundTasks.close(); }
    document.querySelectorAll("#s-tabs button").forEach(x => {
      x.classList.toggle("on", x === b);
      x.setAttribute("aria-selected", String(x === b));
      x.tabIndex = x === b ? 0 : -1;
    });
    for (const t of TABS) $("#tab-" + t).hidden = t !== S.tab;
    b.scrollIntoView({block:"nearest", inline:"nearest"});
    if (S.tab === "topo") drawTopo();
    if (S.tab === "assets") renderAssetLibrary();
    if (S.tab === "challenges") renderChallenges();
    if (S.tab === "vulns") renderVulns();
    if (S.tab === "flags") renderFlags();
    if (S.tab === "experience" && typeof SessionExperience !== "undefined") SessionExperience.load();
    if (S.tab === "chat") { SessionObserver.render(); AgentWorkbench.render(); }
    if (S.tab === "report") loadReport();
    if (S.tab === "deliverables") SessionArchive.load();
  };
});
$("#s-tabs").addEventListener("keydown", event => {
  const buttons = [...document.querySelectorAll("#s-tabs button")].filter(b => !b.hidden);
  const index = buttons.indexOf(document.activeElement);
  if (index < 0) return;
  let next;
  if (event.key === "ArrowRight") next = (index + 1) % buttons.length;
  else if (event.key === "ArrowLeft") next = (index - 1 + buttons.length) % buttons.length;
  else if (event.key === "Home") next = 0;
  else if (event.key === "End") next = buttons.length - 1;
  else return;
  event.preventDefault(); buttons[next].focus(); buttons[next].click();
});

let stateRequestVersion = 0;
let stateLoadingSession = null, stateQueued = false, stateRefreshTimer = null;
let stateRefreshSession = null, stateLastSession = null, stateLastRefresh = 0;
function scheduleStateRefresh() {
  if (!S.cur || document.visibilityState === "hidden") return;
  const sessionId = S.cur;
  if (stateLoadingSession === sessionId) { stateQueued = true; return; }
  if (stateRefreshTimer !== null) {
    if (stateRefreshSession === sessionId) return;
    clearTimeout(stateRefreshTimer);
  }
  // 短突发合并，连续工具事件最多约每 750ms 拉取一次；不拖延到流结束。
  const elapsed = stateLastSession === sessionId ? Date.now() - stateLastRefresh : Infinity;
  stateRefreshSession = sessionId;
  stateRefreshTimer = setTimeout(() => {
    stateRefreshTimer = null; stateRefreshSession = null;
    if (S.cur === sessionId && document.visibilityState !== "hidden") refreshState();
  }, Math.max(200, 750 - elapsed));
}
async function refreshState() {
  if (!S.cur) return;
  if (stateLoadingSession === S.cur) { stateQueued = true; return; }
  const sessionId = S.cur, version = ++stateRequestVersion;
  if (stateRefreshTimer !== null) { clearTimeout(stateRefreshTimer); stateRefreshTimer = null; }
  stateRefreshSession = null; stateQueued = false;
  stateLastSession = sessionId; stateLastRefresh = Date.now();
  stateLoadingSession = sessionId;
  S.polling?.stateRequested();
  try {
    const state = await api(`/api/sessions/${sessionId}/state`);
    if (S.cur !== sessionId || version !== stateRequestVersion) return;
    S.state = state;
    if (typeof ComposerModels !== "undefined") ComposerModels.sync();
    renderSessionHead(); renderChips(); renderApprovals(); renderQuestions();
    if (S.tab === "topo") drawTopo();
    if (S.tab === "assets") renderAssetLibrary();
    if (S.tab === "vulns") renderVulns();
    if (S.tab === "flags") renderFlags();
    if (S.tab === "experience" && typeof SessionExperience !== "undefined") SessionExperience.load();
    renderChallenges();
    SessionObserver.render();
    AgentWorkbench.render();
    ToolParameters.refresh();
    if (S.tab === "deliverables") SessionArchive.refreshIfStale();
    if (TERMINAL.has(state.session?.status) && !S.es) await SessionHistory.syncLatest();
  } catch (e) { if (S.cur === sessionId) toast(e.message, true); }
  finally {
    if (version === stateRequestVersion) {
      stateLoadingSession = null;
      if (stateQueued) { stateQueued = false; scheduleStateRefresh(); }
    }
  }
}

/* 完成标记取自平台；解题中标记取自仍在执行的赛题任务。 */
function renderChallenges() {
  const competition = S.state?.session?.mode === "competition";
  const button = document.querySelector('[data-tab="challenges"]');
  const counter = $("#s-challenge-progress"), box = $("#tab-challenges");
  button.hidden = !competition;
  counter.hidden = !competition;
  const progress = S.state?.challenge_progress;
  const solved = progress?.solved_ids || [], unsolved = progress?.unsolved_ids || [];
  const activeIds = new Set(S.state?.session?.status === "running"
    ? (S.state.tasks || []).filter(task => ["running", "dispatched"].includes(task.status))
      .map(task => task.challenge_id).filter(Boolean) : []);
  const active = unsolved.filter(id => activeIds.has(id));
  const pending = unsolved.filter(id => !activeIds.has(id));
  const total = progress?.total_challenges;
  counter.textContent = progress ? I18n.t`已解出 ${solved.length} / ${total} 题${active.length ? I18n.t` · ${active.length} 题解题中` : ""}` : I18n.t("等待读取题目列表");
  if (competition && S.tab !== "challenges") return;
  const signature = JSON.stringify([S.cur, competition, progress, active]);
  if (box.dataset.signature === signature) return;
  box.dataset.signature = signature;
  if (!competition) { box.replaceChildren(); return; }
  if (!progress) {
    box.innerHTML = I18n.html('<div class="card empty-note">等待读取赛方题目列表，获取后会在这里实时显示完成进度。</div>');
    return;
  }
  const labels = {solved: I18n.t("已解出"), active: I18n.t("解题中"), pending: I18n.t("未完成")};
  const icons = {solved: "✓", active: "▶", pending: "○"};
  const group = (ids, status) => `<section class="challenge-group">
    <h4>${labels[status]}<span>${ids.length}</span></h4>
    ${ids.length ? `<ul class="challenge-grid">${ids.map(id => `<li class="challenge-id ${status}" aria-label="${esc(id)}，${labels[status]}"><span aria-hidden="true">${icons[status]}</span><code>${esc(id)}</code>${status === "active" ? I18n.html('<small class="challenge-active-label">解题中</small>') : ""}</li>`).join("")}</ul>` : `<p class="challenge-empty">${status === "solved" ? I18n.t("暂无已解出的题目") : I18n.t("没有未完成题目")}</p>`}
    </section>`;
  box.innerHTML = I18n.html`<section class="card challenge-board" aria-label="题目完成进度">
    <header class="challenge-heading"><div><h3>题目列表</h3><p>平台确认通关后实时标记</p></div>
      <div class="challenge-score"><strong>${solved.length}<span> / ${total}</span></strong><small>已解出 / 全部题目</small></div></header>
    <progress class="challenge-meter" value="${solved.length}" max="${Math.max(total, 1)}" aria-label="已解出题目进度"></progress>
    ${total ? (active.length ? group(active, "active") : "") + group(solved, "solved") + group(pending, "pending") : I18n.html('<p class="challenge-empty">赛方暂未提供题目。</p>')}
    </section>`;
}

/* 高风险动作即时确认：撞见高风险入口的分支挂起待裁决（G5 交互形态） */
const APV_CN = { reverse_shell: I18n.t("反弹连接"), webshell_upload: I18n.t("上传 WebShell"),
                 container_escape: I18n.t("容器逃逸"), brute_force: I18n.t("口令爆破"),
                 delete_files: I18n.t("删除文件"), intrusive_write: I18n.t("写入操作") };
function renderApprovals() {
  const list = (S.state && S.state.pending_approvals) || [];
  const box = $("#topo-ops-apv"), sessionId = S.cur;
  const signature = JSON.stringify([sessionId, list]);
  if (box.dataset.signature === signature) { updateSessionActions(); return; }
  box.dataset.signature = signature;
  box.innerHTML = list.map(a => I18n.html`
    <div class="apv">
      <div class="apv-title">⚠ 高风险动作待确认：${esc(APV_CN[a.action] || I18n.t("执行外部工具操作"))}</div>
      <p class="apv-purpose">${esc(a.purpose || a.detail || I18n.t("请先核实本次操作的目标和影响。"))}</p>
      <details class="apv-technical"><summary>查看工具与调用信息</summary><div class="apv-detail">${esc(a.detail || "")}</div></details>
      <p class="apv-scope">仅允许本次调用。拒绝后，其余任务继续执行。</p>
      <div class="apv-ops">
        <button class="btn primary sm" data-apv="${esc(a.approval_id)}" data-allow="1">允许本次调用</button>
        <button class="btn sm" data-apv="${esc(a.approval_id)}" data-allow="0">拒绝</button>
      </div>
    </div>`).join("");
  $("#topo-ops-apv").querySelectorAll("[data-apv]").forEach(b => b.onclick = async () => {
    try {
      await api(`/api/sessions/${sessionId}/approvals/${b.dataset.apv}`,
        { method: "POST", body: JSON.stringify({ allow: b.dataset.allow === "1" }) });
      toast(b.dataset.allow === "1" ? I18n.t("已允许本次调用") : I18n.t("已拒绝本次调用"));
      refreshState();
    } catch (e) { toast(e.message, true); refreshState(); }
  });
  updateSessionActions();
}

function updateSessionActions() {
  const panel = $("#session-actions");
  if (!panel) return;
  const wasHidden = panel.hidden;
  panel.hidden = !$("#topo-ops-apv").children.length
    && !$("#topo-ops-ask").children.length;
  // 面板贴在输入坞上方，出现/收起会改变时间线可视高度：原本跟底的保持跟底。
  if (wasHidden !== panel.hidden && traceStick) traceScroll(true);
}

/* 卡片按问题 ID 复用：状态轮询/拓扑重绘不重建输入框、焦点或勾选。 */
const answeringQuestions = new Set();
function renderQuestions() {
  const box = $("#topo-ops-ask");
  if (!box) return;
  const sessionId = S.cur;
  if (box.dataset.sessionId !== sessionId) {
    box.replaceChildren();
    box.dataset.sessionId = sessionId;
  }
  const list = (S.state && S.state.pending_questions) || [];
  const keep = new Set(list.map(q => q.question_id));
  for (const card of [...box.children]) {
    if (!keep.has(card.dataset.questionId)) card.remove();
  }
  list.forEach((q, index) => {
    let card = [...box.children].find(c => c.dataset.questionId === q.question_id);
    if (!card) {
      card = document.createElement("div");
      card.className = "apv ask";
      card.dataset.questionId = q.question_id;
    }
    const signature = JSON.stringify([q.agent, q.question, q.options, q.multiple, q.targets, q.secret]);
    if (card.dataset.signature !== signature) {
      card.dataset.signature = signature;
      const options = q.options || [];
      const targets = q.targets || [];
      const multiple = Boolean(q.multiple);
      card.innerHTML = I18n.html`
        <div class="ask-head">
          <span class="ask-badge" aria-hidden="true">问</span>
          <div class="apv-title">${esc(agentName(q.agent) || q.agent || I18n.t("智能体"))} 向你提问</div>
        </div>
        <div class="apv-detail">${esc(q.question || "")}</div>
        ${options.length ? I18n.html`<div class="ask-block">
          <div class="ask-label">选择你的答复${multiple ? I18n.html`<span class="ask-label-tag">可多选</span>` : ""}</div>
          <div class="ask-ops" role="group" aria-label="${multiple ? I18n.t("候选回答，可选择多项") : I18n.t("候选回答")}">
            ${options.map((option, i) => `<button type="button" class="ask-opt" data-qchoice="${i}"${multiple ? ` aria-pressed="false"` : ""}>
              <span class="ask-opt-idx" aria-hidden="true">${i + 1}</span>
              <span class="ask-opt-text">${esc(option)}</span>
              ${multiple ? `<span class="ask-opt-check" aria-hidden="true">✓</span>` : ""}
            </button>`).join("")}
          </div>
        </div>` : ""}
        ${targets.length ? I18n.html`<div class="ask-block">
          <div class="ask-label">同时授权新目标<span class="ask-label-tag">可选</span></div>
          <div class="ask-targets">
            ${targets.map(raw => {
              const t = typeof raw === "string" ? { target: raw, why: "" } : raw;
              return `<label class="ask-target">
                <input type="checkbox" data-qtarget value="${esc(t.target)}">
                <span class="ask-target-body">
                  <span class="ask-target-host">${esc(t.target)}</span>
                  <span class="ask-target-why">${t.why ? esc(t.why) : I18n.t("本次测试需要使用该目标")}</span>
                </span>
              </label>`;
            }).join("")}
          </div>
          <div class="ask-targets-hint">这些目标目前不在授权范围内。勾选后随本次答复一并加入本次会话的授权范围；不勾选不影响你选择的答复。</div>
        </div>` : ""}
        <div class="ask-free">
          ${q.secret?I18n.html('<input type="password" maxlength="256" autocomplete="one-time-code" aria-label="本次验证码" placeholder="本次验证码，仅用于当前验证" data-qfree>'):`<textarea rows="2" maxlength="1000" aria-label="${I18n.t('你的答复')}" placeholder="${multiple ? I18n.t("可补充说明…（Enter 提交，Shift+Enter 换行）") : I18n.t("或输入自定义答复…（Enter 发送，Shift+Enter 换行）")}" data-qfree></textarea>`}
          <button type="button" class="ask-send" data-qsend>${multiple ? I18n.t("提交答复") : I18n.t("发送答复")}</button>
        </div>
        <div class="ask-status" role="status" aria-live="polite"></div>`;
      const input = card.querySelector("[data-qfree]");
      const sendButton = card.querySelector("[data-qsend]");
      const picked = () => [...card.querySelectorAll("[data-qchoice].selected")]
        .map(button => Number(button.dataset.qchoice));
      // 多选可点选多项后统一提交：按钮文案跟随已选数量，未选时也能只发文字。
      const syncSend = () => {
        if (!multiple) return;
        const count = picked().length;
        sendButton.textContent = count ? I18n.t`提交所选（${count}）` : I18n.t("提交答复");
      };
      const sendText = () => {
        const text = input.value.trim();
        const choices = multiple ? picked() : [];
        const targets = collectTargets(card);
        if (!choices.length && !text && !targets.length) {
          input.focus();
          toast(multiple ? I18n.t("先选择至少一项，或填写答复") : I18n.t("先填写答复内容"), true);
          return;
        }
        const body = choices.length
          ? { choices, add_targets: targets }
          : { text, add_targets: targets };
        if (choices.length && text) body.text = text;
        answerQuestion(q.question_id, body, card, sessionId);
      };
      card.querySelectorAll("[data-qchoice]").forEach(button => button.onclick = () => {
        if (multiple) {
          const on = button.classList.toggle("selected");
          button.setAttribute("aria-pressed", String(on));
          syncSend();
          return;
        }
        answerQuestion(q.question_id, { choice: Number(button.dataset.qchoice),
          add_targets: collectTargets(card) }, card, sessionId);
      });
      sendButton.onclick = sendText;
      input.onkeydown = event => {
        if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
          event.preventDefault();
          event.stopPropagation();
          sendText();
        }
      };
    }
    if (box.children[index] !== card) box.insertBefore(card, box.children[index] || null);
  });
  updateSessionActions();
}
function collectTargets(card) {
  return [...card.querySelectorAll("[data-qtarget]:checked")].map(c => c.value);
}
async function answerQuestion(qid, body, card, sessionId) {
  const key = `${sessionId}:${qid}`;
  if (answeringQuestions.has(key)) return;
  answeringQuestions.add(key);
  const status = card.querySelector(".ask-status");
  const controls = [...card.querySelectorAll("button, input, textarea")];
  controls.forEach(control => { control.disabled = true; });
  status.classList.remove("error");
  status.textContent = I18n.t("正在提交答复…");
  card.setAttribute("aria-busy", "true");
  try {
    await api(`/api/sessions/${sessionId}/questions/${qid}`,
      { method: "POST", body: JSON.stringify(body) });
    if (S.cur === sessionId) {
      ++stateRequestVersion;  // 丢弃提交前的旧快照，防止已答问题重新出现。
      S.state.pending_questions = (S.state.pending_questions || []).filter(q => q.question_id !== qid);
      renderQuestions();
      toast(I18n.t("已答复，智能体将继续"));
      refreshState();
    }
  } catch (e) {
    status.classList.add("error");
    status.textContent = I18n.t`提交失败：${e.message}。答复已保留，可重试。`;
    if (S.cur === sessionId) { toast(e.message, true); refreshState(); }
  } finally {
    answeringQuestions.delete(key);
    controls.forEach(control => { control.disabled = false; });
    card.removeAttribute("aria-busy");
  }
}

/* 仪表盘：实时 token 消耗与会话态势（数据面 = /state） */
const fmtK = n => n >= 1e6 ? (n / 1e6).toFixed(2) + "M"
  : n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n ?? 0);
let dashRenderVersion = 0;
async function renderDash() {
  const box = $("#dash-body");
  if (!box) return;
  const version = ++dashRenderVersion;
  box.setAttribute("aria-busy", "true");
  try {
    const metrics = await api(`/api/dashboard?period=${encodeURIComponent(Dashboard.period)}`);
    if (S.view !== "dash" || version !== dashRenderVersion) return;
    Dashboard.render(box, metrics);
  } catch (e) {
    if (S.view === "dash" && version === dashRenderVersion) Dashboard.error(box, e.message);
  } finally {
    if (version === dashRenderVersion) box.setAttribute("aria-busy", "false");
  }
}

const sessionControlPending = new Set();
const sessionMessagePending = new Set();
function syncComposerControl() {
  const button = $("#comp-send"), input = $("#comp-input");
  const state = S.state?.session, session = state?.session_id === S.cur ? state : null;
  const draft = !!input.value.trim() || !!S.compFiles?.length;
  const active = session && (!TERMINAL.has(session.status) || session.paused);
  const action = draft || !active ? "send" : session.paused ? "resume" : "pause";
  const pending = sessionControlPending.has(S.cur) || sessionMessagePending.has(S.cur);
  const labels = {send:I18n.t("发送消息"), pause:I18n.t("暂停任务"), resume:I18n.t("继续任务")};
  button.dataset.action = action;
  button.setAttribute("aria-label", pending ? I18n.t("正在处理") : labels[action]);
  button.title = pending ? I18n.t("正在处理…") : labels[action];
  button.setAttribute("aria-busy", String(pending));
  button.disabled = !S.cur || pending || (action === "send" && !draft);
  button.classList.toggle("ready", draft || !!active);
  const glyph = action === "pause" ? '<rect x="6" y="6" width="12" height="12" rx="1.5" fill="currentColor" stroke="none"/>'
    : action === "resume" ? '<path d="m9 5 11 7-11 7z" fill="currentColor" stroke="none"/>'
    : '<path d="M12 19V5M5 12l7-7 7 7"/>';
  const signature = `${action}:${pending}`;
  if (button.dataset.signature !== signature) {
    button.dataset.signature = signature;
    button.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${pending ? '<circle cx="12" cy="12" r="7" stroke-dasharray="30 14"/>' : glyph}</svg>`;
  }
  input.disabled = sessionMessagePending.has(S.cur);
  $("#comp-attach").disabled = input.disabled;
}
async function toggleTaskPause() {
  const s = S.state?.session, sessionId = S.cur;
  if (!s || s.session_id !== sessionId || (TERMINAL.has(s.status) && !s.paused) || sessionControlPending.has(sessionId)) return;
  const paused = !s.paused;
  sessionControlPending.add(sessionId);
  syncComposerControl();
  try {
    const updated = await api(`/api/sessions/${encodeURIComponent(sessionId)}/${paused ? "pause" : "resume"}`,
      {method:"POST", body:"{}"});
    if (S.cur !== sessionId) return;
    if (S.state?.session) Object.assign(S.state.session, updated);
    toast(paused ? I18n.t("已请求暂停，当前调用返回后暂停") : I18n.t("已继续原任务"));
  } catch (e) { toast(e.message, true); }
  finally {
    sessionControlPending.delete(sessionId);
    if (S.cur === sessionId) { renderSessionHead(); refreshState(); }
  }
}
function renderSessionHead() {
  const s = S.state.session;
  // 附件清单来自轮询状态（S.state.attachments），详情接口同名字段兜底
  const attachmentsOf = () => S.state?.attachments || s.attachments || [];
  const running = !TERMINAL.has(s.status);
  $("#s-title").textContent = s.title || s.target || s.session_id;
  $("#s-title").title = s.target || "";
  $("#op-rename").onclick = () => startRename(S.cur, $("#s-title"));
  $("#s-meta").innerHTML = [
    `<span class="pill ${s.status}">${STATUS_CN[s.status] || s.status}</span>`,
    running && s.paused ? I18n.html`<span class="pill paused">已暂停</span>` : "",
    `<span class="tag blue">${MODE_CN[s.mode] || s.mode}</span>`,
    // 附件常驻凭据：滑窗滚出首屏后用户仍能确认附件已随题交付（悬浮列名，点击回溯）
    attachmentsOf().length ? I18n.html`<button type="button" class="tag" data-attachments
      title="${esc(attachmentsOf().map(a => a.name).join("\n"))}">附件 ${attachmentsOf().length}</button>` : "",
    I18n.html`<button type="button" class="tag proj-chip" data-move-project title="切换会话所属项目" aria-label="切换项目：${esc(projectName(s.project_id))}" aria-haspopup="dialog"><span>${esc(projectName(s.project_id))}</span><svg class="proj-chevron" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="m4 6 4 4 4-4"/></svg></button>`,
    // 用时扣除暂停与执行间隙区间（门内累计，实时随轮询刷新）
    s.started_at ? I18n.html`<span class="tag">用时 ${fmtDur(s.started_at + (s.excluded_seconds || 0), s.finished_at)}</span>` : "",
    `<span class="tag mono">${esc(s.session_id)}</span>`,
    s.side_session?.parent_session_id ? I18n.html('<button type="button" class="tag" data-side-parent>返回来源会话</button>') : "",
  ].join("");
  const sideParent=$("#s-meta [data-side-parent]");
  if(sideParent) sideParent.onclick=()=>openSession(s.side_session.parent_session_id);
  const attChip=$("#s-meta [data-attachments]");
  if(attChip) attChip.onclick=()=>{
    // 经存档点轨道回溯：命中附件名的横条点击即定位（窗口外自动补页）
    const names=attachmentsOf().map(a => a.name);
    const bar=[...(document.querySelector("#um-rail")?.children || [])]
      .find(el => names.some(n => (el.dataset.preview || "").includes(n)));
    bar?.click();
  };
  const moveProject = $("#s-meta [data-move-project]");
  if (moveProject) moveProject.onclick = () => ProjectsPage.moveSession(S.cur);
  $("#s-err").innerHTML = [
    s.mode === "src" && S.state?.recovery?.unresolved_count > 0
      ? I18n.html`<div class="pause-banner">有 ${Number(S.state.recovery.unresolved_count)} 次操作的结果尚待核对。继续任务时，灵枢会收到这些记录；请先核实效果，避免重复操作。</div>` : "",
    s.error ? `<div class="err-banner">${esc(s.error)}</div>` : "",
  ].join("");
  syncComposerControl();
  $("#comp-input").placeholder = running
    ? I18n.t("补充消息或线索，输入 / 使用命令…")
    : I18n.t("继续对话，或输入 / 使用命令…");
  $("#comp-input").title = running
    ? s.paused ? I18n.t("任务暂停中 · 可补充消息，点击继续任务后推进") : I18n.t("Enter 发送 · Shift+Enter 换行 · / 命令")
    : I18n.t("Enter 发送 · Shift+Enter 换行 · 发送后继续对话");
  if(s.side_session) $("#comp-input").placeholder=I18n.t`与 ${agentName(s.side_session.agent)} 继续旁路对话，补充任务或线索…`;
  const moreOpen = $("#session-more")?.open || false;
  $("#s-ops").innerHTML = I18n.html`
    <details class="session-more" id="session-more" ${moreOpen ? "open" : ""}><summary class="action-icon" title="更多操作" aria-label="更多操作"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/></svg></summary><div class="session-more-menu">
      <a class="btn sm" href="/api/sessions/${encodeURIComponent(S.cur)}/report/download" download="${I18n.t("报告.md")}">下载报告</a>
      <a class="btn sm" href="/api/sessions/${encodeURIComponent(S.cur)}/audit/download" download>导出审计</a>
      <button class="btn sm" id="op-storage">清理存储</button>
      ${running ? I18n.html`<button class="btn danger sm" id="op-stop">结束任务</button>` : ""}
      <button class="btn danger sm" id="op-del" ${running ? "disabled" : ""}>删除会话</button>
    </div></details>`;
  if (running) $("#op-stop").onclick = async () => {
    const sessionId = S.cur;
    try { await api(`/api/sessions/${encodeURIComponent(sessionId)}/stop`, {method:"POST", body:"{}"});
      toast(I18n.t("已结束任务，记录与成果保留")); if(S.cur === sessionId) refreshState(); }
    catch(e) { toast(e.message, true); }
  };
  $("#op-storage").onclick = () => SessionStorage.open(S.cur);
  $("#op-del").onclick = (ev) => armDelete(ev.currentTarget, S.cur);
}

/* Flag 概览：无提交接口时显示「已提取」数；接了判定接口才显示「通过/已提交」 */
function flagChipValue(f) {
  if (!f || !f.total) return "—";
  return f.submitted ? `${f.accepted}/${f.submitted}` : `${f.total}`;
}
function renderChips() {
  const st = S.state;
  const v = st.vulns.by_status || {};
  const mu = st.model_usage || {};
  const tokens = mu.used_tokens ?? mu.total_tokens ?? mu.tokens;
  const rounds = st.decision_rounds ?? (st.round_views || []).length;
  const bc = st.business_context || {};
  const coverage = { observed_transaction_flow: I18n.t("交易流"), public_flow: I18n.t("公开流程"),
                     entry_only: I18n.t("仅入口"), none: I18n.t("无") }[bc.coverage];
  const phase = { queued: I18n.t("等待调度"), starting: I18n.t("启动任务"), mode_routing: I18n.t("判断任务类型"), intent_routing: I18n.t("理解续聊意图"),
    model_preflight: I18n.t("检查模型"), platform_profile: I18n.t("解析平台档案"),
    scope_confirmation: I18n.t("确认测试范围"),
    master_reply: I18n.t("灵枢回复中"), runtime_starting: I18n.t("初始化工具与智能体"),
    executing: I18n.t("执行中"), completed: I18n.t("已完成"), failed: I18n.t("失败"), stopped: I18n.t("已停止"),
    interrupted: I18n.t("已中断") }[TERMINAL.has(st.session.status) ? st.session.status : st.session.phase] || st.session.phase || I18n.t("等待启动");
  const firstChip = st.session.mode === "src" && coverage
    ? [I18n.t("业务覆盖"), coverage] : [I18n.t("当前阶段"), phase];
  $("#s-chips").innerHTML = [
    firstChip,
    [I18n.t("收录漏洞"), v.accepted || 0], [I18n.t("候选/核验中"), (v.candidate || 0) + (v.verifying || 0)],
    ["Flag", flagChipValue(st.flags)],
    [I18n.t("主控轮次"), rounds],
    ["Token", tokens != null ? Number(tokens).toLocaleString(I18n.dateLocale) : "—"],
  ].map(([k, x]) => `<div class="chip"><b>${x}</b>${k}</div>`).join("");
}

/* ================= 拓扑画布 ================= */
const nodeStatus = s => ({ done: "done", completed: "done", active: "active",
                           running: "active" }[String(s || "").toLowerCase()] || "pending");

/* 左→右分层 DAG 布局（dagre 式）：层内重心排序减少交叉，逐层错落堆叠 */
const TOPO_GEO = { NODE_W: 292, GAP_X: 110, GAP_Y: 28, PAD: 44, HUB_GAP: 90 };

function topoReorder(layerIds, preds, succs) {
  const order = new Map();
  const reindex = () => layerIds.forEach(arr => arr.forEach((id, i) => order.set(id, i)));
  reindex();
  for (let pass = 0; pass < 4; pass++) {
    const down = pass % 2 === 0;                     // 下行走前驱、上行走后继
    const seq = down ? layerIds.slice(1) : layerIds.slice(0, -1).reverse();
    for (const arr of seq) {
      const bc = new Map(arr.map(id => {
        const rel = ((down ? preds.get(id) : succs.get(id)) || [])
          .map(x => order.get(x)).filter(v => v !== undefined);
        return [id, rel.length ? rel.reduce((a, b) => a + b, 0) / rel.length : order.get(id)];
      }));
      arr.sort((a, b) => bc.get(a) - bc.get(b));
      arr.forEach((id, i) => order.set(id, i));
    }
  }
  return layerIds;
}

function topoApplyView(box, v) {
  const stage = box.querySelector("#topo-stage");
  if (stage) stage.style.transform = `translate(${v.tx}px, ${v.ty}px) scale(${v.s})`;
}
function topoFit(box, w, h) {
  const live = box.querySelector("#topo-live");
  const reserved = live ? live.offsetTop + live.offsetHeight + 12 : 0;
  const vw = Math.max(box.clientWidth, 240), vh = Math.max(box.clientHeight - reserved, 180);
  const s = Math.min(vw / (w + 28), vh / (h + 28), 1.2);
  return { s, tx: (vw - w * s) / 2, ty: reserved + (vh - h * s) / 2 };
}
function topoZoom(box, k) {
  const v = S.topoView;
  if (!v) return;
  const cx = box.clientWidth / 2, cy = box.clientHeight / 2;
  const ns = Math.min(2.4, Math.max(0.3, v.s * k));
  v.tx = cx - (cx - v.tx) * (ns / v.s);
  v.ty = cy - (cy - v.ty) * (ns / v.s);
  v.s = ns;
  topoApplyView(box, v);
}

function renderTopoActivity() {
  const panel = $("#topo-live");
  if (!panel || !S.state) return;
  const summaries = typeof TaskTrace !== "undefined" ? TaskTrace.progressSnapshot() : [];
  const rows = TopologyLayout.activity(S.state, summaries);
  const status = S.state.session?.status || "created";
  const labels = {running:I18n.t("执行中"),done:I18n.t("已完成"),completed:I18n.t("已完成"),failed:I18n.t("失败"),cancelled:I18n.t("已停止"),stopped:I18n.t("已停止"),interrupted:I18n.t("已中断"),pruned:I18n.t("已暂缓")};
  const hasFacts = S.topoGraph?.nodes.some(node=>node.kind==="finding");
  panel.innerHTML = `<header><b>${TERMINAL.has(status) ? I18n.t("最后推进情况") : I18n.t("当前推进")}</b><span>${esc(labels[status] || status)}</span></header>
    ${rows.length ? rows.map(row=>`<div class="topo-live-row"><strong>${esc(actorName(row.actor || ""))} · ${esc(labels[row.status] || row.status || I18n.t("执行中"))}</strong><p>${esc(row.text || I18n.t("尚未收到阶段说明。"))}</p></div>`).join("") : I18n.html('<p>尚未收到子任务进展，初始化与执行记录见会话页。</p>')}
    ${!hasFacts ? I18n.html('<small>尚未发布有依据的产出事实；正在执行不代表没有推进。</small>') : ''}`;
}

async function loadTopoRules(record, offset = 0) {
  const session=S.cur, key=JSON.stringify([session,record.record_id,record.version]);
  S.topoRuleCache ||= new Map();
  let cached=S.topoRuleCache.get(key);
  const panel=SessionTopology.detailPanel();
  if (!offset && cached) {
    const target=panel?.querySelector(".topo-rule-content");
    if(target) target.textContent=cached.text || I18n.t("该记录未包含用户规则或任务原话。");
  } else {
    try {
      const data=await api(`/api/sessions/${encodeURIComponent(session)}/topology/rules?record_id=${encodeURIComponent(record.record_id)}&offset=${offset}`);
      if(S.cur!==session || panel?._curId!==record.id) return;
      if(offset && cached?.version!==data.version) throw new Error(I18n.t("规则已更新，请重新打开记录"));
      cached={text:(offset ? cached?.text || "" : "")+data.text,next:data.next_offset,version:data.version};
      S.topoRuleCache.set(key,cached);
      panel.querySelector(".topo-rule-content").textContent=cached.text || I18n.t("该记录未包含用户规则或任务原话。");
    } catch(error) {
      if(S.cur===session && panel?._curId===record.id) panel.querySelector(".topo-rule-content").textContent=I18n.t`读取失败：${error.message}`;
      return;
    }
  }
  if(panel?._curId===record.id) {
    const more=panel.querySelector("[data-rules-more]");
    if(more) {more.hidden=cached?.next == null;more.onclick=()=>loadTopoRules(record,cached.next);}
  }
}

function drawTopo() {
  const box = $("#topo");
  if (!S.state) { box.innerHTML = ""; return; }
  const routes = typeof TaskTrace !== "undefined" ? TaskTrace.routeSnapshot() : [];
  SessionTopology.render(S.state, routes);
  renderTopoActivity(); renderApprovals(); renderQuestions();
}

function renderAssetLibrary() {
  if(!S.state)return;
  const routes=typeof TaskTrace!=="undefined" ? TaskTrace.routeSnapshot() : [];
  SessionTopology.render(S.state,routes,true);
}

function topoFocus(id) { SessionTopology.focus(id); }

function topoCollection(group) {
  S.topoQueries ||= {}; S.topoPages ||= {};
  const query = S.topoQueries[group.id] || "";
  const items = group.members.filter(n => `${n.title} ${n.body || ""}`.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  const page = Math.min(S.topoPages[group.id] || 0, Math.max(0, Math.ceil(items.length / 20) - 1));
  S.topoPages[group.id] = page;
  const rows = items.slice(page * 20, page * 20 + 20);
  return I18n.html`<label class="topo-search-label">筛选资料<input data-topo-search type="search" value="${esc(query)}" placeholder="输入地址、标题或记录内容"></label>
    <div class="topo-collection-list">${rows.map(n => `<button data-topo-record="${esc(n.id)}"><b>${esc(n.title)}</b><small>${esc(n.actor || I18n.t("原始记录"))} · ${esc(n.record_id || "")}</small></button>`).join("") || I18n.html('<p class="td-note">没有匹配的资料</p>')}</div>
    <div class="topo-pagination"><button data-topo-page="-1" ${page === 0 ? "disabled" : ""}>上一页</button><span>${items.length ? page*20+1 : 0}–${Math.min((page+1)*20, items.length)} / ${items.length}</span><button data-topo-page="1" ${(page+1)*20 >= items.length ? "disabled" : ""}>下一页</button></div>`;
}

function topoRecordDetail(p) {
  if(p.projection || p.semantic_group) return SessionTopology.projectionDetail(p)
    + (p.record_id && p.body ? I18n.html`<details class="topo-original"><summary>原探索记录</summary><pre>${esc(p.body)}</pre></details>` : "");
  if(p.kind==="exploration") {
    const states={open:I18n.t("待探索"),running:I18n.t("探索中"),blocked:I18n.t("受阻"),supported:I18n.t("有支持"),refuted:I18n.t("已反驳"),exhausted:I18n.t("已走不通"),closed:I18n.t("已结束")};
    const relations=[...(S.topoGraph?.edges || []),...(S.topoGraph?.links || [])].filter(e=>e.source===p.id || e.target===p.id);
    const ids=[...new Set(relations.map(e=>e.source===p.id ? e.target : e.source))];
    return I18n.html`<p class="td-note">${esc(states[p.state] || p.state)} · 探索记录，状态由 Agent 维护。</p>
      <p class="td-note topo-fact-text">${esc(p.title)}</p>${p.reason ? I18n.html`<p class="td-note">原因：${esc(p.reason)}</p>` : ""}
      <div class="topo-related"><b>资产、父探索与产出</b>${ids.map(id=>`<button data-topo-record="${esc(id)}">${esc(S.topoProcess.get(id)?.title || id)}</button>`).join("") || I18n.html('<p>尚无显式关联</p>')}</div>
      <details class="topo-original"><summary>完整探索记录</summary><pre>${esc(p.body || "")}</pre></details>`;
  }
  if(p.kind==="milestone") return I18n.html`<p class="td-note">路线推进说明；属于 Agent 当时的观察与计划，不自动代表已验证事实。</p><p class="td-note topo-rule-content">${esc(p.body)}</p>`;
  if(p.kind==="rule") return I18n.html('<p class="td-note topo-rule-content">正在读取原文…</p><button class="btn sm" data-rules-more hidden>继续读取原文</button>');
  const source = p.provenance?.producer || {};
  let payload={};try{payload=JSON.parse(p.body || '{}');}catch{/* 历史记录仍按原文显示。 */}
  const attrs=payload.attrs || {},fingerprint=payload.fingerprint || {};
  const scriptIntel=p.node_type==='script' ? I18n.html`<section class="topo-related"><b>脚本情报</b>
    <p class="td-note"><code>SHA256 ${esc(fingerprint.sha256 || I18n.t('未记录'))}</code></p>
    <p class="td-note">静态接口 ${Number(attrs.route_count || (attrs.routes || []).length)} 条 · 秘密引用 ${(attrs.secrets || []).length} 条 · ${esc(attrs.verification_status || I18n.t('待核实'))}</p>
    ${(attrs.routes || []).length ? `<details><summary>静态接口线索</summary><pre>${esc(attrs.routes.join('\n'))}</pre></details>` : ''}
    ${[...(attrs.parameters || []),...(attrs.config_keys || [])].length ? `<details><summary>参数与配置键</summary><pre>${esc([...(attrs.parameters || []),...(attrs.config_keys || [])].join('\n'))}</pre></details>` : ''}
    ${(attrs.algorithm_calls || []).length ? `<details><summary>算法调用证据</summary><pre>${esc(attrs.algorithm_calls.join('\n'))}</pre></details>` : ''}
    ${attrs.analysis && Object.keys(attrs.analysis).length ? `<details><summary>逆向分析</summary><pre>${esc(JSON.stringify(attrs.analysis,null,2))}</pre></details>` : ''}
    ${attrs.script_ref ? `<p class="td-note">脱敏源码引用：<code>${esc(attrs.script_ref)}</code> · Agent 可分页读取</p>` : ''}
    ${attrs.source_map_url ? `<p class="td-note">Source map：${esc(attrs.source_map_url)}${attrs.source_index != null ? ` · #${esc(String(attrs.source_index))}` : ''}</p>` : ''}
    ${(attrs.secrets || []).length ? `<div>${attrs.secrets.map(secret=>`<p class="td-note"><code>${esc(secret.secret_ref)}</code> · ${esc(secret.kind)} · ${esc(secret.status || I18n.t('待核实'))} <button type="button" class="btn sm" data-secret-ref="${esc(secret.secret_ref)}" data-secret-target="${esc(payload.value || p.title)}">查看明文（会记录）</button></p>`).join('')}</div>` : ''}
  </section>` : '';
  const origins = [source.agent_id || p.actor, source.tool_name, source.step_id].filter(Boolean);
  const relations = [...(S.topoGraph?.edges || []),...(S.topoGraph?.links || []),...(S.topoGraph?.assets?.edges || [])].flatMap(e => e.references || [e]).filter(e => e.target === p.id || e.source === p.id
    || p.relation && e.record_id === p.record_id);
  const related = [...new Set(relations.flatMap(e => e.source === p.id ? [e.target] : e.target === p.id ? [e.source] : [e.source,e.target]))];
  const collection = S.topoGraph?.groups.find(g => g.members.some(n => n.id === p.id));
  const assetRelations = [...(S.topoProcess?.values() || [])].filter(n=>n.relation &&
    [n.source_ref,n.target_ref].includes(`${p.layer}:${p.record_id}`));
  const isAsset=S.topoGraph?.assets?.nodes.some(n=>n.id===p.id);
  return I18n.html`${collection ? I18n.html`<button class="topo-back" data-topo-record="${esc(collection.id)}">← 返回${esc(collection.title)}</button>` : ""}<div class="td-agent">${esc(origins.join(" · ") || I18n.t("未记录来源"))}</div>
    <p class="td-note topo-fact-text">${esc(p.title)}</p>
    ${scriptIntel}
    ${isAsset && p.layer && p.record_id ? `<section class="asset-test-summary" data-asset-tests-summary><h4>${I18n.t('关联测试记录')}</h4><p class="muted">${I18n.t('正在读取…')}</p></section>` : ''}
    ${assetRelations.length ? I18n.html`<div class="topo-related"><b>资产关系与依据</b>${assetRelations.map(n=>`<button data-topo-record="${esc(n.id)}">${esc(n.title)}</button>`).join("")}</div>` : ''}
    ${related.length ? I18n.html`<div class="topo-related"><b>明确引用的关联记录</b>${related.map(id => `<button data-topo-record="${esc(id)}">${esc(S.topoProcess?.get(id)?.title || id)}</button>`).join("")}</div>` : I18n.html('<p class="td-note">暂无明确的事实依赖关系。</p>')}
    ${isAsset && p.layer && p.record_id ? `<button type="button" class="btn sm" data-archive-ref="${esc(p.layer+':'+p.record_id)}">${I18n.t('查看原始记录与版本')}</button>` : I18n.html`<details class="topo-original"><summary>查看完整记录与来源</summary><p class="td-note">${esc(p.layer || "")} · ${esc(p.record_id || "")} · v${esc(String(p.version || ""))}</p><pre>${esc(p.body || I18n.t("规则原文保存在黑板，按引用读取。"))}</pre></details>`}`;
}

window.addEventListener("resize", () => { if (S.tab === "topo" && S.state) drawTopo(); });

/* ================= 任务/漏洞/Flag ================= */
/* ================= 事件流 ================= */
function evLine(e) {
  const t = evElapsed(e.ts);
  const de = typeof e.detail === "object" ? JSON.stringify(e.detail) : String(e.detail ?? "");
  return `<div class="ev"><span class="ts">${esc(t)}</span><span class="ty">${esc(e.type || "")}</span>
    <span class="ac">${esc(e.actor || "")}</span><span class="de">${esc(de.slice(0, 220))}</span></div>`;
}

function showNodeDetail(nodeId) {
  if(S.tab==='topo' && S.topoGraph?.assets?.nodes.some(n=>n.id===nodeId)) {
    SessionTopology.openAssets(nodeId);return;
  }
  const panel = SessionTopology.detailPanel();
  if (!panel) return;
  if (panel.hidden === false && panel._curId === nodeId) {
    panel.hidden = true; panel._curId = null; topoFocus(null); return;
  }
  panel._curId = nodeId;
  panel.hidden = false;
  let title = nodeId, body = "";
  const taskDetail = t => {
    let h = `<div class="td-task">
      <div class="td-agent">${esc(agentName(t.agent_type))} · ${esc(t.status || "")}</div>`;
    for (const r of t.routes || []) {
      h += `<div class="td-route">
        <div class="td-route-h"><b>${esc(r.name || "")}</b>
          <span class="mono">${Math.round((r.progress || 0) * 100)}%</span></div>
        ${r.note ? `<div class="td-note">${esc(r.note)}</div>` : ""}
      </div>`;
    }
    return h + `</div>`;
  };
  if (nodeId.startsWith("__process_")) {
    const p=S.topoProcess?.get(nodeId);
    if(p) {
      title=p.title;
      body = p.kind === "collection" ? topoCollection(p) : topoRecordDetail(p);
    }
  } else if (nodeId.startsWith("__route_")) {
    // 路线分支节点：执行现场全量呈现（agent / 状态 / 进度 / 最新笔记）
    const r = (S.topoRouteMeta || new Map()).get(nodeId);
    if (r) {
      title = `${agentName(r.agent)} · ${r.name || I18n.t("路线")}`;
      const pct = Math.round((r.progress || 0) * 100);
      body = `<div class="td-task">
        <div class="td-route-h"><b>${esc(r.status || "")}</b><span class="mono">${pct}%</span></div>
        <div class="prog ${pct === 100 ? "done" : ""}"><i style="width:${pct}%"></i></div>
        ${r.note ? `<div class="td-note">${esc(r.note)}</div>`
                : I18n.html('<div class="td-empty">路线尚无现场笔记</div>')}
      </div>`;
    }
  } else if (nodeId.startsWith("__finding_")) {
    // 发现节点：完整内容与出处（哪条路线/哪一轮写下——黑板记录直读）
    const f = (S.topoFindings || new Map()).get(nodeId);
    if (f) {
      title = ({ note: I18n.t("共享笔记"), flag: "Flag", asset: I18n.t("入口 / 资产") })[f.type] || I18n.t("发现");
      const who = [f.agent && agentName(f.agent), f.route,
                   f.round != null ? I18n.t`第 ${f.round} 轮` : ""].filter(Boolean).join(" · ");
      body = `<div class="td-task">
        ${who ? I18n.html`<div class="td-agent">出处：${esc(who)}</div>` : ""}
        ${f.url ? `<div class="td-note" style="word-break:break-all">${esc(f.url)}</div>` : ""}
        <div class="td-note" style="white-space:pre-wrap">${esc(f.text || "")}</div></div>`;
    }
  } else if (nodeId === "__orphan__") {
    title = I18n.t("下发任务");
    const list = S.topoOrphans || [];
    body = list.map(taskDetail).join("");
  }
  if (!body) {
    // DAG 方向节点：列出挂在该节点下的任务与路线
    const nodes = (S.state?.topology?.dag_nodes || []);
    const node = nodes.find(n => n.node_id === nodeId);
    title = node?.title || nodeId;
    const tasks = (S.state?.topology?.tasks || []).filter(t => t.parent_dag_node === nodeId);
    body = tasks.length ? tasks.map(taskDetail).join("")
                        : I18n.html('<div class="td-empty">暂无任务数据</div>');
  }
  panel.querySelector(".td-title").textContent = title;
  panel.querySelector(".td-body").innerHTML = body;
  topoFocus(nodeId);
  const record=S.topoProcess?.get(nodeId);
  if(record && typeof CoveragePage!=='undefined')CoveragePage.assetSummary(record,panel);
  if(record?.kind==="rule") loadTopoRules(record);
  panel.querySelector(".td-close").onclick = () => { panel.hidden = true; panel._curId = null; topoFocus(null); };
  panel.querySelectorAll("[data-topo-record]").forEach(button => {
    button.onclick = () => {panel._curId = null;showNodeDetail(button.dataset.topoRecord);};
  });
  if (typeof AssetSecretViewer !== "undefined") AssetSecretViewer.bind(panel);
  panel.querySelectorAll('[data-archive-ref]').forEach(button=>button.onclick=()=>SessionArchive.openReference(button.dataset.archiveRef));
  panel.querySelectorAll("[data-topo-page]").forEach(button => {
    button.onclick = () => {S.topoPages[nodeId] += Number(button.dataset.topoPage);panel._curId=null;showNodeDetail(nodeId);};
  });
  const search = panel.querySelector("[data-topo-search]");
  if (search) search.oninput = () => {
    const position = search.selectionStart;
    S.topoQueries[nodeId] = search.value; S.topoPages[nodeId] = 0;
    panel._curId = null; showNodeDetail(nodeId);
    const fresh = panel.querySelector("[data-topo-search]");fresh.focus();fresh.setSelectionRange(position, position);
  };
}

let vulnPageSession = null, vulnOffset = 0, vulnRows = null, vulnLoading = false, vulnGeneration = 0;
async function loadVulnPage(offset) {
  const session = S.cur, version = ++vulnGeneration;
  vulnLoading = true; renderVulns();
  try {
    const page = await api(`/api/sessions/${encodeURIComponent(session)}/vulnerabilities?offset=${offset}&limit=50`);
    if (S.cur !== session || version !== vulnGeneration) return;
    vulnOffset = offset; vulnRows = page.items;
  } catch (error) { if (S.cur === session) toast(I18n.t`漏洞读取失败：${error.message}`, true); }
  finally { if (S.cur === session && version === vulnGeneration) {vulnLoading = false;renderVulns();} }
}
function renderVulns() {
  if (vulnPageSession !== S.cur) {vulnPageSession=S.cur;vulnOffset=0;vulnRows=null;vulnLoading=false;vulnGeneration++;}
  const vs = vulnOffset ? (vulnRows || []) : (S.state.vulns.items || []);
  const root=$("#tab-vulns"),signature=JSON.stringify([S.cur,vs,S.state.vulns.total,vulnOffset,vulnLoading]);
  if(root._content===signature)return;
  const previous=VulnerabilityDetails.capture(root);
  root._content=signature;
  $("#tab-vulns").innerHTML = vs.length ? `<div class="grid-cards">${vs.map(v => {
    const [sevCn, sevCls] = SEV_CN[v.severity] || [v.severity || I18n.t("未定级"), "blue"];
    return `<div class="card vcard">
      <div class="h"><b>${esc(v.title || v.vuln_type || I18n.t("漏洞"))}</b>
        <span class="tag ${sevCls}">${sevCn}</span>
        <span class="tag ${v.status === "accepted" ? "green" : ""}">${esc(({candidate:I18n.t("待独立复测"),verifying:I18n.t("鉴真复测中"),inconclusive:I18n.t("证据不足 · 未收录"),accepted:I18n.t("确认收录"),rejected_false_positive:I18n.t("误报 · 不收录"),rejected_compliance:I18n.t("不符合规则 · 不收录")})[v.status] || v.status || "")}</span>
        ${v.poc_verified ? I18n.html`<span class="tag gold">PoC 已验证</span>` : ""}</div>
      ${v.endpoint ? `<div class="ep">${esc(v.endpoint)}</div>` : ""}
      ${v.vulnerability_ids?.length ? `<p>${esc(v.vulnerability_ids.join(" · "))}</p>` : ""}
      ${v.verification?.reason ? `<p>${esc(v.verification.reason)}</p>` : ""}
      ${v.verification?.rule_quote ? I18n.html`<p class="muted">危害规则：${esc(v.verification.rule_quote)}</p>` : ""}
      <div class="muted" style="font-size:11.5px">${esc(v.vuln_type || "")}${v.cvss_score != null ? " · CVSS " + v.cvss_score : ""} · ${esc(v.vuln_id || "")}</div>
      ${VulnerabilityDetails.html(v)}
    </div>`; }).join("")}</div>`
    : I18n.html`<div class="empty-note">尚无漏洞记录</div>`;
  const total = S.state.vulns.total ?? vs.length;
  VulnerabilityDetails.mount(root,vs,previous);
  if (total > 50 || vulnOffset) {
    $("#tab-vulns").insertAdjacentHTML("afterbegin", I18n.html`<div class="vuln-pagination"><span>共 ${total} 条 · 当前 ${vulnOffset+1}–${vulnOffset+vs.length}</span><button class="btn sm" data-vuln-prev ${vulnLoading || !vulnOffset ? "disabled" : ""}>上一页</button><button class="btn sm" data-vuln-next ${vulnLoading || vulnOffset+vs.length>=total ? "disabled" : ""}>下一页</button><button class="btn sm" data-vuln-refresh ${vulnLoading ? "disabled" : ""}>${vulnLoading ? I18n.t("读取中…") : I18n.t("刷新当前页")}</button></div>`);
    $("[data-vuln-prev]").onclick=()=>loadVulnPage(Math.max(0,vulnOffset-50));
    $("[data-vuln-next]").onclick=()=>loadVulnPage(vulnOffset+50);
    $("[data-vuln-refresh]").onclick=()=>loadVulnPage(vulnOffset);
  }
}
function renderFlags() {
  const f = S.state.flags;
  $("#tab-flags").innerHTML = f && f.items.length ? `<div class="grid-cards">${f.items.map(x => `
    <div class="card flag-card">
      <div class="v">${esc(x.value)}</div>
      <div><span class="tag ${x.accepted ? "gold" : x.submitted ? "blue" : "green"}">
        ${x.accepted ? I18n.t("已判定") : x.submitted ? I18n.t("已提交") : I18n.t("已提取")}</span>
        <span class="muted" style="font-size:11.5px"> ${esc(x.challenge_id || "")}</span></div>
    </div>`).join("")}</div>`
    : I18n.html`<div class="empty-note">尚无 Flag 记录</div>`;
}

/* ---- 解题轨迹：按真实发生顺序还原成一条可读时间线 ---- */
let traceStick = true;
let traceRenderTarget = null;
const TRACE_AGENT_SLOTS = new Map();
const TRACE_TURNS = new Map();
function traceBeginRender() {
  const live = $("#trace-box"), stage = document.createElement("div");
  stage.className = "trace trace-staging";
  stage.setAttribute("aria-hidden", "true");
  stage.style.width = `${live.getBoundingClientRect().width}px`;
  stage.style.height = `${live.clientHeight}px`;
  live.parentNode.append(stage);
  traceRenderTarget = stage;
  return stage;
}
function traceEndRender(stage, commit) {
  if (traceRenderTarget === stage) traceRenderTarget = null;
  if (commit) $("#trace-box").replaceChildren(...stage.childNodes);
  stage.remove();
}
function traceReset({preservePanels = false} = {}) {
  if (traceScrollFrame !== null) cancelAnimationFrame(traceScrollFrame);
  traceScrollFrame = null; traceForceScroll = false;
  if (!preservePanels) { SessionObserver.reset(); AgentWorkbench.reset(); }
  TaskTrace.reset();
  (traceRenderTarget || $("#trace-box")).innerHTML = I18n.html('<div class="empty-note">等待任务开始</div>');
  TRACE_AGENT_SLOTS.clear();
  TRACE_TURNS.clear();
  S.think.clear();
  traceStick = true;
  $("#trace-jump").hidden = true;
}
function traceClock(ts) {
  return evElapsed(ts);
}
const fmtSize = n => n == null || n === "" ? "" :
  n >= 1048576 ? (n / 1048576).toFixed(1) + "MB"
  : n >= 1024 ? Math.round(n / 1024) + "KB" : n + "B";
function traceActorSlot(actor) {
  actor = String(actor || "unknown");
  if (!TRACE_AGENT_SLOTS.has(actor))
    TRACE_AGENT_SLOTS.set(actor, (TRACE_AGENT_SLOTS.size % 8) + 1);
  return TRACE_AGENT_SLOTS.get(actor);
}
function traceRow(e, body, cls = "") {
  const actor = String(e.actor || "");
  const agentCls = actor && actor !== "flag_manager" ? ` ag-${traceActorSlot(actor)}` : "";
  return `<div class="tl ${cls}${agentCls}" data-agent="${esc(actor)}">
    <time class="t">${esc(traceClock(e.ts))}</time><div class="b">${body}</div></div>`;
}
function traceAppend(e, body, cls = "") {
  const box = traceRenderTarget || $("#trace-box");
  box.querySelector(".empty-note")?.remove();
  box.insertAdjacentHTML("beforeend", traceRow(e, body, cls));
  traceScroll();
  const row = box.lastElementChild;
  if (row.classList.contains("tl-turn")) TRACE_TURNS.set(String(e.actor || ""), row);
  row.dataset.eventTs = e.ts || "";
  row.dataset.agent = String(e.actor || "");
  return row;
}
let traceScrollFrame = null, traceForceScroll = false;
function traceScroll(force = false) {
  traceForceScroll ||= force;
  if (traceScrollFrame !== null) return;
  traceScrollFrame = requestAnimationFrame(() => {
    traceScrollFrame = null;
    const mustScroll = traceForceScroll;
    traceForceScroll = false;
    traceScrollNow(mustScroll);
  });
}
function traceScrollNow(force = false) {
  const box = $("#trace-box");
  if (!box) return;
  if (force || traceStick) {
    box.scrollTop = box.scrollHeight;
    traceStick = true;
  }
  $("#trace-jump").hidden = traceStick || box.scrollHeight <= box.clientHeight + 2;
}
function traceTurn(actor) {
  const row = TRACE_TURNS.get(String(actor || ""));
  return row?.isConnected ? row : null;
}
function traceCloseThinking(turn) {
  turn?.querySelectorAll(".tl-think.live").forEach(el => {
    el.classList.remove("live");
    const summary = el.querySelector("summary");
    if (summary) summary.textContent = I18n.t("思考过程");
  });
}
function traceFinishThinking(turn) {
  traceCloseThinking(turn);
  // 回合收尾：流式期间的纯文本替换为 Markdown 渲染（表格/代码块/列表）。
  // 已渲染的块标记 .md，后续文本段另起新块，不再往里拼原文
  turn?.querySelectorAll(".tl-say:not(.md)").forEach(el => {
    el.classList.add("md");
    el.innerHTML = mdRender(el.textContent || "");
  });
}
function argBrief(name, args) {
  args = args && typeof args === "object" ? args : {};
  if (args._redacted) return I18n.t("正在读取参数");
  const one = v => String(v ?? "").replace(/\s+/g, " ").trim();
  if (name === "skill_open") return one(args.skill_id);
  if (name === "skill_read") return `${one(args.skill_id)} / ${one(args.path)}`;
  if (name === "skill_search") return one(args.query || args.domain || I18n.t("浏览技能目录"));
  if (name === "http_request") {
    let target = one(args.path || args.url || "/");
    try { if (/^https?:\/\//i.test(target)) target = new URL(target).pathname + new URL(target).search; } catch {}
    return `${one(args.method || "GET").toUpperCase()} ${target}`.slice(0, 180);
  }
  if (name === "shell") return one(args.command).slice(0, 180);
  if (name === "python") return one(args.code).split("\n")[0].slice(0, 180) || I18n.t("运行脚本");
  if (name === "note") return one(args.text).slice(0, 180);
  if (name === "analyze_text") return I18n.t("分析题面文本");
  if (name === "analyze_attachments") return I18n.t("分析全部附件");
  if (name.startsWith("mcp__")) {
    const parts = name.split("__");
    const server = parts[1] || "mcp";
    const tool = parts.slice(2).join("__") || "?";
    const first = Object.values(args).find(v =>
      (typeof v === "string" || typeof v === "number" || typeof v === "boolean") &&
      String(v).trim() !== "");
    return `${one(server)} · ${one(tool)}${first !== undefined ? `(${one(String(first)).slice(0, 60)})` : ""}`;
  }
  const compact = one(JSON.stringify(args));
  return compact === "{}" ? I18n.t("执行") : compact.slice(0, 180);
}
/* 工具足迹条：回合尾逐行列出模型实际使用的工具与命令原文 */
const TOOLS_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>';
function toolFootprint(name, args) {
  args = args && typeof args === "object" ? args : {};
  if (args._redacted) return I18n.t("参数见调用卡片");
  const cut = s => { s = String(s ?? "").trim(); return s.length > 400 ? s.slice(0, 400) + " …" : s; };
  if (name === "shell") return cut(args.command);
  if (name === "python") return cut(String(args.code ?? "").split("\n")[0]);
  if (name === "http_request") {
    let target = String(args.path || args.url || "/");
    try { if (/^https?:\/\//i.test(target)) { const u = new URL(target); target = u.host + u.pathname + u.search; } } catch {}
    return cut(`${String(args.method || "GET").toUpperCase()} ${target}`);
  }
  return cut(argBrief(name, args));
}
function traceToolsStrip(turn) {
  const flow = turn?.querySelector(".flow");
  if (!flow) return null;
  let strip = flow.querySelector(".tl-tools");
  if (!strip) {
    flow.insertAdjacentHTML("beforeend", I18n.html`<div class="tl-tools"><div class="th">${TOOLS_SVG}
      <span>已使用 <b class="n">0</b> 个工具</span></div><div class="lines"></div></div>`);
    strip = flow.lastElementChild;
  }
  flow.append(strip);   // 始终钉在回合流末尾，后续动作卡插在其前
  return strip;
}
function tracePush(e, {observe = true, eventId = null} = {}) {
  const box = traceRenderTarget || $("#trace-box");
  if (!box) return;
  const d = (typeof e.detail === "object" && e.detail) || {};
  const actor = String(e.actor || "");
  if (observe) { SessionObserver.push(e); AgentWorkbench.push(e); }
  const sideSession=S.state?.session?.side_session || S.sessions?.find(s=>s.session_id===S.cur)?.side_session;
  if(sideSession && !["user.message","user.attachment","agent.reply","agent.reply_failed","engagement.paused","engagement.resumed","system.lifecycle"].includes(e.type)) return;
  if (typeof TaskTrace !== "undefined" && !sideSession && TaskTrace.push(e, eventId)) return;
  switch (e.type) {
    case "context.compaction_requested":
      traceAppend(e, I18n.html`<div class="m">已请求手动压缩，等待下一次模型调用。</div>`, "tl-sys");
      break;
    case "context.manual_compacted":
      traceAppend(e, I18n.html`<div class="m">${esc(actorName(actor))} · 上下文整理：约 ${esc(d.estimated_tokens_before)} → ${esc(d.estimated_tokens_after)} tokens（离线估算）${d.changed ? "" : I18n.t(" · 当前没有可进一步缩小的旧历史")}</div>`, "tl-sys");
      break;
    case "task.start":
      traceAppend(e, I18n.html`<div class="m">开始执行 · ${esc(actorName(actor))}</div>`, "tl-sys");
      break;
    case "agent.turn": {
      traceFinishThinking(traceTurn(actor));
      const turn = traceAppend(e, I18n.html`<div class="hd"><span class="who">${esc(actorName(actor))}</span>
        <span class="k">第 ${esc(d.turn || "?")} 回合${d.forced_finish ? I18n.t(" · 正在收尾") : ""}</span></div>
        ${d.turn === 1 && d.angle ? `<div class="ang">${esc(d.angle)}</div>` : ""}<div class="flow"></div>`, "tl-turn");
      turn.dataset.turn = String(d.turn || "");
      break;
    }
    case "agent.delta": {
      const kind = d.kind || "text";
      if (kind === "tool_call") break;
      const turn = traceTurn(actor) || traceAppend(e,
        I18n.html`<div class="hd"><span class="who">${esc(actorName(actor))}</span><span class="k">本页回合片段</span></div><div class="flow"></div>`, "tl-turn tl-frag");
      if (kind === "reset") {
        turn.querySelectorAll(".tl-say,.tl-think,.tl-thinkchip").forEach(el => el.remove());
        const rec = S.think.get(actor);
        if (rec) { rec.chars = 0; rec.tail = ""; }
        break;
      }
      if (kind === "tool_call") break;
      const flow = turn.querySelector(".flow");
      if (!flow) break;
      if (kind === "reasoning") {
        const text = d.text || "";
        if (!text) break;
        // 工作台显示流式正文，拓扑保留推理摘要；时间线留计数章避免重复刷屏。
        let rec = S.think.get(actor);
        if (!rec) { rec = { chars: 0, tail: "" }; S.think.set(actor, rec); }
        rec.chars += text.length;
        rec.tail = (rec.tail + text).slice(-6000);
        let chip = flow.querySelector(".tl-thinkchip");
        if (!chip) {
          flow.insertAdjacentHTML("beforeend",
            I18n.html('<div class="tl-thinkchip" title="在上方子 Agent 工作台查看实时分析"></div>'));
          chip = flow.lastElementChild;
        }
        chip.textContent = I18n.t`推理 ${fmtK(rec.chars)} 字`;
        break;
      }
      const text = d.text || "";
      if (!text) break;
      traceCloseThinking(turn);   // 文本开始：只关思考气泡，不渲染半截文本
      let el = flow.querySelector(".tl-say:last-child");
      if (el && el.classList.contains("md")) el = null;   // 已渲染块不回填原文
      if (!el) {
        flow.insertAdjacentHTML("beforeend", '<div class="tl-say"></div>');
        el = flow.lastElementChild;
      }
      el._publicRaw = (el._publicRaw || '') + text;
      el.textContent = TaskTrace.commentaryText(el._publicRaw);
      traceScroll();
      break;
    }
    case "agent.tool_call": {
      const turn = traceTurn(actor) || traceAppend(e,
        I18n.html`<div class="hd"><span class="who">${esc(actorName(actor))}</span><span class="k">工具执行</span></div><div class="flow"></div>`, "tl-turn tl-frag");
      traceFinishThinking(turn);
      const flow = turn.querySelector(".flow");
      for (const c of (d.calls || [])) {
        flow.insertAdjacentHTML("beforeend", I18n.html`<div class="tl-act" data-id="${esc(c.id || "")}">
          <button class="hd" type="button" aria-expanded="false"><span class="nm" title="${esc(c.name || "?")}">${esc(SessionObserver.toolName(c.name))}</span>
            <span class="ar">${esc(c.preview || argBrief(c.name, c.arguments))}</span><span class="st">进行中</span>
            <span class="cv" aria-hidden="true">⌄</span></button><div class="nt"></div><pre class="tl-output"></pre></div>`);
        const act = flow.lastElementChild;
        const btn = act.querySelector("button.hd");
        btn.onclick = () => {
          act.classList.toggle("open");
          btn.setAttribute("aria-expanded", String(act.classList.contains("open")));
        };
        ToolParameters.attach(act, c);
      }
      // 工具足迹：计数逐次累加，命令逐行追加（回放时按事件序重建）
      const strip = traceToolsStrip(turn);
      if (strip && (d.calls || []).length) {
        const n = strip.querySelector(".n");
        const lines = strip.querySelector(".lines");
        for (const c of (d.calls || [])) {
          n.textContent = String(Number(n.textContent || "0") + 1);
          lines.insertAdjacentHTML("beforeend",
            `<div class="ln"><span class="tn">${esc(c.name || "?")}</span><code>${esc(c.preview || toolFootprint(c.name, c.arguments))}</code></div>`);
        }
      }
      traceScroll();
      break;
    }
    case "agent.tool_result": {
      const id = CSS.escape(String(d.id || ""));
      let el = id ? box.querySelector(`.tl-act[data-id="${id}"]`) : null;
      if (!el) {
        const turn = traceTurn(actor);
        const acts = turn?.querySelectorAll(".tl-act") || [];
        el = acts[acts.length - 1];
      }
      if (!el) {
        traceAppend(e, I18n.html`<details><summary>${esc(SessionObserver.toolName(d.name || I18n.t("工具")))} · ${d.ok ? I18n.t("已返回") : I18n.t("失败")}（调用见更早记录）</summary><pre>${esc(SessionObserver.cleanText(d.result || d.note || I18n.t("未记录原始响应")))}</pre></details>`, "tl-sys");
        break;
      }
      const st = el.querySelector(".st");
      st.textContent = d.ok ? I18n.t("已返回") : I18n.t("失败");
      st.className = "st " + (d.ok ? "y" : "n");
      el.querySelector(".nt").textContent = SessionObserver.resultSummary(d);
      el.querySelector(".tl-output").textContent = SessionObserver.cleanText(d.result || d.note || I18n.t("未记录原始响应"));
      if (!d.ok) {
        el.classList.add("open");
        el.querySelector("button.hd").setAttribute("aria-expanded", "true");
      }
      traceScroll();
      break;
    }
    case "agent.conclude": {
      const turn = traceTurn(actor);
      traceFinishThinking(turn);
      if (turn) turn.querySelector(".flow").insertAdjacentHTML("beforeend",
        `<div class="tl-concl md">${mdRender(d.summary || "")}</div>`);
      else traceAppend(e, `<div class="hd"><span class="who">${esc(actorName(actor))}</span></div>
        <div class="flow"><div class="tl-concl md">${mdRender(d.summary || "")}</div></div>`, "tl-turn");
      traceScroll();
      break;
    }
    case "agent.note": {
      const turn = traceTurn(actor);
      if (turn) turn.querySelector(".flow").insertAdjacentHTML("beforeend",
        `<div class="tl-share">${esc(d.text || "")}</div>`);
      else traceAppend(e, `<div class="hd"><span class="who">${esc(actorName(actor))}</span></div>
        <div class="flow"><div class="tl-share">${esc(d.text || "")}</div></div>`, "tl-turn");
      traceScroll();
      break;
    }
    case "agent.error": {
      const turn = traceTurn(actor);
      traceFinishThinking(turn);
      if (turn) turn.querySelector(".flow").insertAdjacentHTML("beforeend",
        `<div class="tl-err">${esc(d.error || "")}</div>`);
      else traceAppend(e, `<div class="hd"><span class="who">${esc(actorName(actor))}</span></div>
        <div class="flow"><div class="tl-err">${esc(d.error || "")}</div></div>`, "tl-turn");
      traceScroll();
      break;
    }
    case "flag.extracted":
      traceAppend(e, I18n.html`<div class="m">捕获 Flag · ${esc(d.value_preview || "")}</div>`, "tl-flag");
      break;
    case "model.started":
      traceAppend(e, I18n.html`<div class="m">${esc(actorName(actor))} · 正在调用 ${esc(modelDisplayName(d.model_key || d.model || I18n.t("模型"), d.provider_ref))} · ${esc(SessionObserver.toolName(d.task_type || I18n.t("任务决策")))}</div>`, "tl-sys");
      break;
    case "model.call": {
      // 调用收尾即定格 Markdown：文本流已结束，不等下一次回合边界
      // （否则纯推理续轮/会话收尾前的流式正文会一直保持原文）
      traceFinishThinking(traceTurn(actor));
      const n = SessionObserver.usageFor(e, S.state);
      const row = traceAppend(e, I18n.html`<div class="m"><strong>${esc(modelDisplayName(d.model_key || d.model || I18n.t("模型"), d.provider_ref))} 已返回</strong> · <span class="model-tokens">${n == null ? I18n.t("单次用量未记录") : `${Number(n).toLocaleString(I18n.dateLocale)} token`}</span> · ${(Number(d.latency_ms || 0) / 1000).toFixed(1)} 秒${d.task_type ? ` · ${esc(d.task_type)}` : ""}</div>`, "tl-sys tl-model");
      row.dataset.callTs = e.ts || "";
      break;
    }
    case "model.failed":
    case "task.failed":
    case "llm.step_fallback":
      traceFinishThinking(traceTurn(actor));   // 调用异常收尾：已流入的正文同样要定格
      traceAppend(e, `<div class="tl-err">${esc(actorName(actor))} · ${esc(d.error || I18n.t("执行未成功，等待重新决策"))}</div>`, "tl-sys");
      break;
    case "model.cancelled":
      traceFinishThinking(traceTurn(actor));
      traceAppend(e, I18n.html`<div class="m">${esc(actorName(actor))} · 模型调用已取消，本次用量未返回</div>`, "tl-sys");
      break;
    case "task.cancelled":
    case "task.pruned":
      traceAppend(e, `<div class="m">${esc(actorName(actor))} · ${e.type === "task.cancelled" ? I18n.t("任务已撤销") : I18n.t("路线已剪枝")}</div>`, "tl-sys");
      break;
    case "task.done":
      traceFinishThinking(traceTurn(actor));
      traceAppend(e, I18n.html`<div class="m">${esc(actorName(actor))} 已完成任务</div>`, "tl-sys");
      break;
    case "user.message": {
      const row = traceAppend(e, `<div class="bubble"><div class="btxt">${esc(d.content || "")}</div></div>`, "tl-user");
      if (d.message_id) row.dataset.messageId = String(d.message_id);
      break;
    }
    case "system.lifecycle":
      if (d.action === "session_kickoff")
        traceAppend(e, `<div class="m">${sideSession ? I18n.t("子 Agent 正在准备回复…") : I18n.t("灵枢开工准备中——加载任务配置…")}</div>`, "tl-sys");
      break;
    case "engagement.paused":
      traceAppend(e, I18n.html`<div class="m">已暂停，当前步骤完成后停止。</div>`, "tl-sys");
      break;
    case "agent.waiting":
      traceAppend(e, I18n.html`<div class="m">${d.kind === "shell" ? I18n.t`等待后台 Shell ${esc(d.job_id || "")}：${esc(d.condition || I18n.t("进程退出"))}` : I18n.t`等待定时唤醒：${esc(new Date(d.wake_at * 1000).toLocaleString(I18n.dateLocale, {hour12:false}))}`} · Agent 已挂起，不消耗模型调用</div>`, "tl-sys");
      break;
    case "shell.notification":
      traceAppend(e, I18n.html`<div class="m">后台任务「${esc(d.label || d.job_id)}」${d.kind === "condition" ? (d.matched ? I18n.t("已命中输出条件") : I18n.t("结束时未命中条件")) : I18n.t`已结束 · 退出码 ${esc(d.returncode ?? "—")}`}，可在「后台任务」查看输出。</div>`, "tl-sys");
      break;
    case "agent.wait_ready":
      traceAppend(e, `<div class="m">${d.timed_out ? I18n.t("监测等待已超时，后台进程仍可继续运行") : d.kind === "shell" && !d.matched ? I18n.t("后台进程已结束，未匹配等待值") : I18n.t("等待条件已满足，Agent 继续任务")}</div>`, "tl-sys");
      break;
    case "engagement.resumed":
      traceAppend(e, I18n.html`<div class="m">已继续${d.paused_seconds ? I18n.t`（暂停 ${fmtElapsed(d.paused_seconds)}，不计入用时）` : ""}</div>`, "tl-sys");
      break;
    case "system.gap":
      // 执行间隙（合盖待机/Ctrl+Z/VM 快照）：时间线如实标记，区间不计入用时
      traceAppend(e, I18n.html`<div class="m">💤 检测到执行间隙 ${fmtElapsed(d.gap_seconds || 0)}——控制台刚刚未在运行，该区间不计入用时</div>`, "tl-sys");
      break;
    case "user.attachment": {
      // 用户上传的文件同样以文件卡片呈现：可直接再预览/下载（attachments/ 一直在）
      const rel = d.rel || (d.name ? `attachments/${d.name}` : "");
      const row = traceAppend(e, `<div class="bubble">${FileCards.card({ rel, name: d.name, size: d.size, side: "user" })}</div>`, "tl-user");
      if (d.message_id) row.dataset.messageId = String(d.message_id);
      break;
    }
    case "file.shared": {
      // 智能体/用户把产出文件发进会话：左侧文件卡片，可在线预览或下载
      const who = actorName(actor) || I18n.t("灵枢");
      const seal = actor && actor !== "master" ? String(who).slice(0, 1) : I18n.t("灵");
      traceAppend(e, `<i class="seal" aria-label="${esc(who)}">${esc(seal)}</i>
        <div class="bubble"><div class="who">${esc(who)}</div>
        ${FileCards.card({ rel: d.path, name: d.name, size: d.size })}
        ${d.note ? `<div class="btxt fc-note">${esc(d.note)}</div>` : ""}</div>`, "tl-master tl-conv");
      break;
    }
    case "report.ready": {
      // 异步报告完成：作为灵枢的文件消息出现，点开即可阅读/下载
      const rname = String(d.path || "").split("/").pop() || I18n.t("解题报告.md");
      traceAppend(e, I18n.html`<i class="seal" aria-label="灵枢">灵</i>
        <div class="bubble"><div class="who">灵枢</div><div class="btxt">解题报告已生成</div>
        ${FileCards.card({ rel: `reports/${rname}`, name: rname, size: d.size })}</div>`, "tl-master tl-conv");
      break;
    }
    case "verification.queued":
      traceAppend(e, I18n.html`<div class="m">候选入口已交给鉴真独立复测 · ${esc(d.vuln_id)}</div>`, "tl-sys");
      break;
    case "verification.started":
      traceAppend(e, I18n.html`<div class="hd"><span class="who">鉴真</span><span class="k">独立危害复测</span></div><p>${esc(d.title || d.vuln_id)}</p>`, "tl-master");
      break;
    case "verification.result":
      traceAppend(e, I18n.html`<details class="secretary-card" open><summary>鉴真 · ${esc(({confirmed:I18n.t("确认危害，收录"),false_positive:I18n.t("误报，不收录"),out_of_rules:I18n.t("不符合规则，不收录"),inconclusive:I18n.t("证据不足，未收录")})[d.verdict] || I18n.t("复测完成"))}</summary><p>${esc(d.title || "")}</p><p>${esc(d.reason || "")}</p>${d.rule_quote ? I18n.html`<p>用户危害规则：${esc(d.rule_quote)}</p>` : ""}${[...(d.evidence || []),...(d.controls || [])].map(ref=>`<p class="mono">${esc(ref.execution_id)} · ${esc(ref.quote)}</p>`).join("")}</details>`, "tl-master");
      break;
    case "verification.error":
      traceAppend(e, I18n.html`<div class="m">独立复测未完成，不收录：${esc(d.error || I18n.t("原因未提供"))}</div>`, "tl-sys");
      break;
    case "secretary.suggested":
      traceAppend(e, I18n.html`<details class="secretary-card"><summary>秘书 · ${(d.suggestions || []).length} 条参谋建议</summary>${(d.suggestions || []).map(item => I18n.html`<article><strong>${esc(item.summary)}</strong><p>${esc(item.benefit)}</p><small>不确定性：${esc(item.uncertainty || I18n.t("未说明"))}</small><p class="mono">${esc((item.evidence_refs || []).join(" · "))}</p></article>`).join("") || I18n.html('<p>本次没有需要补充的建议。</p>')}</details>`, "tl-master");
      break;
    case "secretary.decided":
      traceAppend(e, I18n.html`<details class="secretary-card"><summary>灵枢 · 已评估秘书建议</summary>${(d.decisions || []).map(item => `<article><strong>${esc(({accepted:I18n.t("采纳"),partial:I18n.t("部分采纳"),rejected:I18n.t("拒绝"),needs_evidence:I18n.t("待补证")})[item.verdict] || I18n.t("待评估"))}</strong><p>${esc((d.suggestions || []).find(x=>x.id===item.id)?.summary || item.id)}</p><p>${esc(item.reason)}</p></article>`).join("")}</details>`, "tl-master");
      break;
    case "secretary.failed":
      traceAppend(e, I18n.html('<div class="m">秘书咨询未完成，灵枢沿用原决策继续。</div>'), "tl-sys");
      break;
    case "ring.steer":
      traceAppend(e, I18n.html`<div class="hd"><span class="who">灵枢</span>
        <span class="k">→ ${esc(agentName(d.agent))}</span></div>
        <div class="tl-reply">${esc(d.hint || "")}</div>`, "tl-master");
      break;
    case "ring.steer_feedback":
      traceAppend(e, I18n.html`<div class="hd"><span class="who">${esc(agentName(e.actor))}</span>
        <span class="k">督导回执 · ${esc(STEERING_STATUS_CN[d.status] || d.status || "")}</span></div>
        <div class="m">${esc(d.detail || I18n.t("当前指令已送入本任务的模型请求"))}</div>`, "tl-master");
      break;
    case "decision.change": {
      if (d.action === "llm_mode_route") {
        const verdict = (d.intent === "dialogue" || d.intent === "rules")
          ? (d.intent === "dialogue" ? I18n.t("对话") : I18n.t("规则"))
          : (MODE_CN[d.llm_mode] || d.llm_mode || "?");
        traceAppend(e, I18n.html`<div class="hd"><span class="who">灵枢</span>
          <span class="k">任务模式判定</span></div>
          <div class="ang">→ ${esc(verdict)}${d.rationale ? ` · ${esc(d.rationale)}` : ""}</div>`, "tl-master");
      } else if (d.action === "dispatch_plan" || d.action === "dispatch_plan_skipped") {
        traceAppend(e, I18n.html`<div class="m">灵枢已定下发计划（任务复杂度 · 路线预算 · 思考强度）</div>`, "tl-sys");
      } else if (d.action === "meta_plan") {
        const n = Array.isArray(d.new_nodes) ? d.new_nodes.length : 0;
        traceAppend(e, I18n.html`<div class="hd"><span class="who">${esc(actorName(actor))}</span>
          <span class="k">第 ${esc(d.round ?? "?")} 轮规划${n ? I18n.t` · ${n} 个方向节点` : ""}</span></div>
          ${d.analysis ? `<div class="ang">${esc(String(d.analysis))}</div>` : ""}`, "tl-master");
      } else if (d.action === "review") {
        // 复盘结论优先显示灵枢自己的话（用户追问的答案也走这里）
        const body = String(d.llm_reason || "").trim()
          || (d.continue ? I18n.t("继续迭代") : I18n.t`收敛：${d.stop_reason || I18n.t("目标达成")}`);
        traceAppend(e, I18n.html`<div class="hd"><span class="who">${esc(actorName(actor))}</span>
          <span class="k">第 ${esc(d.round ?? "?")} 轮复盘</span></div>
          <div class="ang md">${mdRender(body)}</div>`, "tl-master");
      } else if (d.action === "user_feed_consumed") {
        const cn = { steering: I18n.t("派发督导"), review: I18n.t("轮末复盘") }[d.channel] || I18n.t("决策");
        traceAppend(e, I18n.html`<div class="tl-reply">已收到你的意见，纳入本轮${cn}。</div>`, "tl-master");
      }
      break;
    }
    case "agent.reply":
      traceAppend(e, `<i class="seal" aria-label="${esc(d.name || actorName(actor))}">${esc((d.name || I18n.t("子")).slice(0,1))}</i><div class="bubble"><div class="who">${esc(d.name || actorName(actor))}</div><div class="btxt md">${mdRender(d.text || "")}</div></div>`, "tl-master tl-conv tl-agent-conv");
      break;
    case "agent.reply_failed":
      traceAppend(e, `<div class="bubble"><div class="btxt">${esc(d.error || I18n.t("子 Agent 回复失败，请重试"))}</div></div>`, "tl-master tl-conv");
      break;
    case "master.reply":
      // Agent 直达回复采用统一正文，名章与角色名标识来源。
      traceAppend(e, I18n.html`<i class="seal" aria-label="灵枢">灵</i><div class="bubble">
        <div class="who">灵枢</div>
        <div class="btxt md">${mdRender(d.text || "")}</div></div>`, "tl-master tl-conv");
      break;
    case "master.reply_failed":
      // 应答通道失败必须可见——否则用户消息像被吞掉
      traceAppend(e, I18n.html`<i class="seal" aria-label="灵枢">灵</i><div class="bubble">
        <div class="btxt">回复失败：${esc(d.error || I18n.t("未知原因"))}。消息已保存，可重试。</div></div>`, "tl-master tl-conv");
      break;
    case "agent.spawn":
      traceAppend(e, I18n.html`<div class="m">🔮 派生分支 · ${esc(d.child || "")}</div>`, "tl-sys");
      break;
    case "agent.spawn_done":
      traceAppend(e, I18n.html`<div class="m">🔮 分支 ${esc(d.child || "")} 收尾（Flag ${d.flags || 0}）</div>`, "tl-sys");
      break;
    case "agent.ask_user":
      traceAppend(e, I18n.html`<div class="tl-ask">向你提问 · ${esc(d.question || "")}</div>`, "tl-sys");
      break;
  }
}
const traceBox = $("#trace-box");
traceBox.addEventListener("scroll", () => {
  if (SessionHistory.isRendering?.()) return;
  const gap = traceBox.scrollHeight - traceBox.scrollTop - traceBox.clientHeight;
  traceStick = gap < 24;
  $("#trace-jump").hidden = traceStick || traceBox.scrollHeight <= traceBox.clientHeight + 2;
}, {passive: true});
$("#trace-jump").onclick = () => SessionHistory.isPast() ? SessionHistory.latest() : traceScroll(true);
function renderAuditEvent(e, {observe = true, eventId = null} = {}) {
  tracePush(e, {observe, eventId});
  if (observe && e.type !== "agent.delta") {
    const box = $("#events");
    box.insertAdjacentHTML("afterbegin", evLine(e));
    while (box.children.length > 250) box.lastChild.remove();
  }
}
function startStreams(rebuild = true) {
  stopStreams();
  if (!S.cur) return;
  if (rebuild) { S.evSeen = 0; SessionHistory.open(); return; }
  S.esTick = Date.now();
  // 滚动历史与 SSE 共用字节游标，重连直接 seek，不从文件头重扫。
  const sessionId = S.cur;
  const es = new EventSource(`/api/sessions/${sessionId}/events`
    + `?cursor=bytes&offset=${S.evSeen || 0}`);
  S.es = es;
  let received = S.evSeen;
  // 审计帧是具名事件（event: audit），onmessage 收不到——必须按事件名监听
  es.addEventListener("audit", m => {
    if (S.cur !== sessionId || S.es !== es) return;
    S.esTick = Date.now();
    try {
      const e = JSON.parse(m.data);
      // 去重游标用 SSE 事件 id（完整日志行的结尾字节偏移）；
      // 载荷 seq 是各写入实例各自计数，多写入者会撞车回退，不能用于去重
      const lid = Number(m.lastEventId) || 0;
      if (lid) {
        if (lid <= received) return;
        received = lid;
      }
      SessionHistory.enqueue(e, lid, sessionId);
      if (/^(?:agent\.(?:discovery|progress|tool_result)|task\.|decision\.|vuln\.|flag\.|platform\.|system\.|model\.call)/.test(e.type || "")) scheduleStateRefresh();
    } catch {}
  });
  es.addEventListener("done", async () => {
    await SessionHistory.drain();
    if (S.cur !== sessionId || S.es !== es) return;
    // 终态排空后主动关闭，防止 EventSource 每隔数秒重连并重复刷新状态。
    stopStreams();
    refreshState();
  });
  es.onerror = () => {};
  // 断流看门狗：合盖休眠/网络切换后 EventSource 会静默僵死（readyState 不可信），
  // 会话仍在运行却长时间无帧即判死，增量重连补齐缺口（已渲染行保留）
  S.esDog = setInterval(() => {
    if (S.view !== "session" || !S.cur || SessionHistory.isPast()) return;
    if (TERMINAL.has(S.state?.session?.status)) return;
    // 暂停态无帧是正常态（智能体已挂起），豁免看门狗避免空转重连
    if (S.state?.session?.paused) return;
    const dead = !S.es || S.es.readyState === EventSource.CLOSED;
    const stale = Date.now() - (S.esTick || 0) > 45000;
    if (dead || stale) startStreams(false);
  }, 15000);
}
function stopStreams() {
  SessionHistory.clearLiveQueue();
  if (S.es) { S.es.close(); S.es = null; }
  if (S.esDog) { clearInterval(S.esDog); S.esDog = null; }
}
// 唤醒即自救：回到前台立刻刷状态，连接不新鲜就增量重连
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return;
  S.polling?.wake();
  if (S.view !== "session" || !S.cur || SessionHistory.isPast()) return;
  if (TERMINAL.has(S.state?.session?.status)) return;
  // 暂停态同样豁免：连接不再也要刷新，但不为无帧空转重连
  if (S.state?.session?.paused) return;
  if (!S.es || S.es.readyState !== EventSource.OPEN || Date.now() - (S.esTick || 0) > 10000)
    startStreams(false);
});

/* ================= 报告 ================= */
async function loadReport() {
  const box = $("#tab-report");
  box.innerHTML = I18n.html`<div class="empty-note">加载中…</div>`;
  try {
    const session = S.cur;
    const d = await api(`/api/sessions/${session}/report`);
    if(S.cur!==session) return;
    if(d.status && d.status!=="done" && !d.markdown) {
      box.innerHTML=`<div class="empty-note">${d.status==="failed" ? I18n.t`报告生成失败：${esc(d.error || I18n.t("未知错误"))}` : I18n.t("答案调查已结束，报告正在后台生成；经验提炼同时进行。")}</div>`;
      if(d.status!=="failed") setTimeout(()=>{if(S.cur===session && S.tab==="report") loadReport();},2000);
      return;
    }
    box.innerHTML = `<div class="card report">${mdRender(d.markdown)}</div>`;
  } catch (e) {
    box.innerHTML = `<div class="empty-note">${esc(e.message)}</div>`;
  }
}

