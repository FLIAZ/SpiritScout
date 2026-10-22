/* ================= 视图：会话详情/拓扑/报告 ================= */
/* ================= 会话详情 ================= */
const TABS = ["chat", "topo", "challenges", "vulns", "flags", "events", "deliverables", "report"];
$("#mobile-sessions-toggle").onclick = () => {
  const open = document.querySelector(".side").classList.toggle("mobile-sessions-open");
  $("#mobile-sessions-toggle").setAttribute("aria-expanded", String(open));
};
function openSession(id) {
  if (S.cur !== id) {
    S.state = null;
    renderChallenges();
    $("#topo-ops-apv").replaceChildren();
    delete $("#topo-ops-apv").dataset.signature;
    $("#topo-ops-ask").replaceChildren();
    updateSessionActions();
  }
  S.cur = id; S.tab = "chat"; S.evSeen = 0; S.topoView = null;
  SessionArchive.reset(id);
  ToolParameters.reset(id);
  if (window.innerWidth <= 860) {
    document.querySelector(".side").classList.remove("mobile-sessions-open");
    $("#mobile-sessions-toggle").setAttribute("aria-expanded", "false");
  }
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
    document.querySelectorAll("#s-tabs button").forEach(x => {
      x.classList.toggle("on", x === b);
      x.setAttribute("aria-selected", String(x === b));
      x.tabIndex = x === b ? 0 : -1;
    });
    for (const t of TABS) $("#tab-" + t).hidden = t !== S.tab;
    b.scrollIntoView({block:"nearest", inline:"nearest"});
    if (S.tab === "topo") drawTopo();
    if (S.tab === "challenges") renderChallenges();
    if (S.tab === "vulns") renderVulns();
    if (S.tab === "flags") renderFlags();
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
function scheduleStateRefresh() {
  if (document.visibilityState === "hidden") return;
  if (stateRefreshTimer) return;
  stateRefreshTimer = setTimeout(() => { stateRefreshTimer = null; refreshState(); }, 120);
}
async function refreshState() {
  if (!S.cur) return;
  if (stateLoadingSession === S.cur) { stateQueued = true; return; }
  const sessionId = S.cur, version = ++stateRequestVersion;
  stateLoadingSession = sessionId;
  S.polling?.stateRequested();
  try {
    const state = await api(`/api/sessions/${sessionId}/state`);
    if (S.cur !== sessionId || version !== stateRequestVersion) return;
    S.state = state;
    renderSessionHead(); renderChips(); renderApprovals(); renderQuestions();
    if (S.tab === "topo") drawTopo();
    if (S.tab === "vulns") renderVulns();
    if (S.tab === "flags") renderFlags();
    renderChallenges();
    SessionObserver.render();
    ToolParameters.refresh();
    if (S.tab === "deliverables") SessionArchive.refreshIfStale();
  } catch (e) { if (S.cur === sessionId) toast(e.message, true); }
  finally {
    if (version === stateRequestVersion) {
      stateLoadingSession = null;
      if (stateQueued) { stateQueued = false; scheduleStateRefresh(); }
    }
  }
}

/* 题目面板直接展示平台完成标记，不按 Flag 数量猜测已解题数。 */
function renderChallenges() {
  const competition = S.state?.session?.mode === "competition";
  const button = document.querySelector('[data-tab="challenges"]');
  const counter = $("#s-challenge-progress"), box = $("#tab-challenges");
  button.hidden = !competition;
  counter.hidden = !competition;
  const progress = S.state?.challenge_progress;
  const solved = progress?.solved_ids || [], unsolved = progress?.unsolved_ids || [];
  const total = progress?.total_challenges;
  counter.textContent = progress ? `已解出 ${solved.length} / ${total} 题` : "等待读取题目列表";
  if (competition && S.tab !== "challenges") return;
  const signature = JSON.stringify([S.cur, competition, progress]);
  if (box.dataset.signature === signature) return;
  box.dataset.signature = signature;
  if (!competition) { box.replaceChildren(); return; }
  if (!progress) {
    box.innerHTML = '<div class="card empty-note">等待读取赛方题目列表，获取后会在这里实时显示完成进度。</div>';
    return;
  }
  const group = (ids, done) => `<section class="challenge-group">
    <h4>${done ? "已解出" : "未完成"}<span>${ids.length}</span></h4>
    ${ids.length ? `<ul class="challenge-grid">${ids.map(id => `<li class="challenge-id ${done ? "solved" : "pending"}" aria-label="${esc(id)}，${done ? "已解出" : "未完成"}"><span aria-hidden="true">${done ? "✓" : "○"}</span><code>${esc(id)}</code></li>`).join("")}</ul>` : `<p class="challenge-empty">${done ? "暂无已解出的题目" : "没有未完成题目"}</p>`}
    </section>`;
  box.innerHTML = `<section class="card challenge-board" aria-label="题目完成进度">
    <header class="challenge-heading"><div><h3>题目列表</h3><p>平台确认通关后实时标记</p></div>
      <div class="challenge-score"><strong>${solved.length}<span> / ${total}</span></strong><small>已解出 / 全部题目</small></div></header>
    <progress class="challenge-meter" value="${solved.length}" max="${Math.max(total, 1)}" aria-label="已解出题目进度"></progress>
    ${total ? group(solved, true) + group(unsolved, false) : '<p class="challenge-empty">赛方暂未提供题目。</p>'}
    </section>`;
}

/* 高风险动作即时确认：撞见高风险入口的分支挂起待裁决（G5 交互形态） */
const APV_CN = { reverse_shell: "反弹连接", webshell_upload: "上传 WebShell",
                 container_escape: "容器逃逸", brute_force: "口令爆破",
                 delete_files: "删除文件", intrusive_write: "写入操作" };
function renderApprovals() {
  const list = (S.state && S.state.pending_approvals) || [];
  const box = $("#topo-ops-apv"), sessionId = S.cur;
  const signature = JSON.stringify([sessionId, list]);
  if (box.dataset.signature === signature) { updateSessionActions(); return; }
  box.dataset.signature = signature;
  box.innerHTML = list.map(a => `
    <div class="apv">
      <div class="apv-title">⚠ 高风险动作待确认：${esc(APV_CN[a.action] || a.action)}</div>
      <div class="apv-detail">${esc(a.detail || "")}<br>仅允许本次调用；其他调用仍需分别确认。拒绝只拦截本次调用，其余分支照常推进。</div>
      <div class="apv-ops">
        <button class="btn primary sm" data-apv="${esc(a.approval_id)}" data-allow="1">允许本次调用</button>
        <button class="btn sm" data-apv="${esc(a.approval_id)}" data-allow="0">拒绝</button>
      </div>
    </div>`).join("");
  $("#topo-ops-apv").querySelectorAll("[data-apv]").forEach(b => b.onclick = async () => {
    try {
      await api(`/api/sessions/${sessionId}/approvals/${b.dataset.apv}`,
        { method: "POST", body: JSON.stringify({ allow: b.dataset.allow === "1" }) });
      toast(b.dataset.allow === "1" ? "已允许本次调用" : "已拒绝本次调用");
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
    const signature = JSON.stringify([q.agent, q.question, q.options, q.multiple, q.targets]);
    if (card.dataset.signature !== signature) {
      card.dataset.signature = signature;
      const options = q.options || [];
      const targets = q.targets || [];
      const multiple = Boolean(q.multiple);
      card.innerHTML = `
        <div class="ask-head">
          <span class="ask-badge" aria-hidden="true">问</span>
          <div class="apv-title">${esc(agentName(q.agent) || q.agent || "智能体")} 向你提问</div>
        </div>
        <div class="apv-detail">${esc(q.question || "")}</div>
        ${options.length ? `<div class="ask-block">
          <div class="ask-label">选择你的答复${multiple ? `<span class="ask-label-tag">可多选</span>` : ""}</div>
          <div class="ask-ops" role="group" aria-label="${multiple ? "候选回答，可选择多项" : "候选回答"}">
            ${options.map((option, i) => `<button type="button" class="ask-opt" data-qchoice="${i}"${multiple ? ` aria-pressed="false"` : ""}>
              <span class="ask-opt-idx" aria-hidden="true">${i + 1}</span>
              <span class="ask-opt-text">${esc(option)}</span>
              ${multiple ? `<span class="ask-opt-check" aria-hidden="true">✓</span>` : ""}
            </button>`).join("")}
          </div>
        </div>` : ""}
        ${targets.length ? `<div class="ask-block">
          <div class="ask-label">同时授权新目标<span class="ask-label-tag">可选</span></div>
          <div class="ask-targets">
            ${targets.map(raw => {
              const t = typeof raw === "string" ? { target: raw, why: "" } : raw;
              return `<label class="ask-target">
                <input type="checkbox" data-qtarget value="${esc(t.target)}">
                <span class="ask-target-body">
                  <span class="ask-target-host">${esc(t.target)}</span>
                  <span class="ask-target-why">${t.why ? esc(t.why) : "本次测试需要使用该目标"}</span>
                </span>
              </label>`;
            }).join("")}
          </div>
          <div class="ask-targets-hint">这些目标目前不在授权范围内。勾选后随本次答复一并加入本次会话的授权范围；不勾选不影响你选择的答复。</div>
        </div>` : ""}
        <div class="ask-free">
          <textarea rows="2" maxlength="1000" aria-label="你的答复" placeholder="${multiple ? "可补充说明…（Enter 提交，Shift+Enter 换行）" : "或输入自定义答复…（Enter 发送，Shift+Enter 换行）"}" data-qfree></textarea>
          <button type="button" class="ask-send" data-qsend>${multiple ? "提交答复" : "发送答复"}</button>
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
        sendButton.textContent = count ? `提交所选（${count}）` : "提交答复";
      };
      const sendText = () => {
        const text = input.value.trim();
        const choices = multiple ? picked() : [];
        const targets = collectTargets(card);
        if (!choices.length && !text && !targets.length) {
          input.focus();
          toast(multiple ? "先选择至少一项，或填写答复" : "先填写答复内容", true);
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
  status.textContent = "正在提交答复…";
  card.setAttribute("aria-busy", "true");
  try {
    await api(`/api/sessions/${sessionId}/questions/${qid}`,
      { method: "POST", body: JSON.stringify(body) });
    if (S.cur === sessionId) {
      ++stateRequestVersion;  // 丢弃提交前的旧快照，防止已答问题重新出现。
      S.state.pending_questions = (S.state.pending_questions || []).filter(q => q.question_id !== qid);
      renderQuestions();
      toast("已答复，智能体将继续");
      refreshState();
    }
  } catch (e) {
    status.classList.add("error");
    status.textContent = `提交失败：${e.message}。答复已保留，可重试。`;
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
  let metrics;
  try {
    metrics = await api("/api/dashboard");
  } catch (e) {
    if (S.view === "dash" && version === dashRenderVersion) toast(e.message, true);
    return;
  }
  if (S.view !== "dash" || version !== dashRenderVersion) return;
  const list = S.sessions || [];
  const running = metrics.running || 0;
  const tokens = metrics.total_tokens || 0;
  const cost = metrics.total_cost || 0;
  const flagCell = f => !f || !f.total ? "—"
    : (f.submitted ? `${f.accepted}/${f.submitted}` : `${f.total}`);

  // 与列表、状态页共用后端用量来源；不把整场用量记在会话开始那一天。
  const daily = metrics.daily || {};
  const dailyArr = Object.entries(daily).sort((a, b) => a[0].localeCompare(b[0]));
  const maxDaily = Math.max(...dailyArr.map(([, v]) => v), 1);

  // 按模型聚合
  const modelAgg = metrics.per_model || {};
  const modelArr = Object.entries(modelAgg).sort((a, b) => b[1] - a[1]);
  const maxModel = Math.max(...modelArr.map(([, v]) => v), 1);

  // 发现台账
  const findings = (S.cur && S.state && S.state.findings) || [];
  // 经验沉淀：跨会话共享经验库（提炼自历史会话，供下次遇到同场景时参考）
  const exps = (metrics.experience && metrics.experience.items) || [];
  const expTotal = (metrics.experience && metrics.experience.total) || exps.length;

  box.innerHTML = `
    <div class="tiles">
      <div class="tile"><span class="tk">token</span><b class="num">${fmtK(tokens)}</b><span class="mu">全部会话累计</span></div>
      <div class="tile"><span class="tk">费用</span><b class="num">$${cost.toFixed(3)}</b><span class="mu">按单价估算</span></div>
      <div class="tile"><span class="tk">会话</span><b class="num">${list.length}</b><span class="mu">${running} 个运行中</span></div>
      <div class="tile"><span class="tk">Flag</span><b class="num">${list.reduce((a, x) => a + (x.flag_stats?.total || 0), 0)}</b><span class="mu">全部提取</span></div>
    </div>
    <div class="dash-sec">每日 Token 消耗</div>
    ${dailyArr.length ? `<div class="bar-chart">${dailyArr.map(([day, t]) => `
      <div class="bar-row"><span class="bar-label mono">${day}</span>
        <div class="bar-track"><i style="width:${Math.round(t / maxDaily * 100)}%"></i></div>
        <span class="bar-val mono">${fmtK(t)}</span></div>`).join("")}</div>`
      : '<div class="empty-note" style="padding:16px 0">暂无数据</div>'}
    <div class="dash-sec">按模型消耗</div>
    ${modelArr.length ? `<div class="bar-chart">${modelArr.map(([m, t]) => `
      <div class="bar-row"><span class="bar-label mono">${esc(m)}</span>
        <div class="bar-track"><i style="width:${Math.round(t / maxModel * 100)}%"></i></div>
        <span class="bar-val mono">${fmtK(t)}</span></div>`).join("")}</div>`
      : '<div class="empty-note" style="padding:16px 0">暂无模型调用</div>'}
    <div class="dash-sec">会话明细</div>
    ${list.length ? `<table class="list"><thead><tr>
      <th>会话</th><th>模式</th><th>状态</th><th>轮次</th><th>token</th><th>费用</th><th>Flag</th><th>用时</th></tr></thead>
      <tbody>${list.map(x => `<tr class="dash-row" data-open="${esc(x.session_id)}">
        <td>${esc(x.title || x.target || x.session_id)}</td>
        <td>${esc(MODE_CN[x.mode] || x.mode || "")}</td>
        <td><span class="pill ${x.status}">${STATUS_CN[x.status] || x.status}</span></td>
        <td class="mono">${x.rounds || 0}</td>
        <td class="mono">${fmtK(x.tokens || 0)}</td>
        <td class="mono">$${Number(x.cost_usd || 0).toFixed(3)}</td>
        <td class="mono">${esc(flagCell(x.flag_stats))}</td>
        <td class="mono">${fmtDur(x.started_at ? x.started_at + (x.excluded_seconds || 0) : x.started_at, x.finished_at)}</td></tr>`).join("")}</tbody></table>`
      : '<div class="empty-note">还没有会话——回「任务」页下达第一个任务</div>'}
    ${findings.length ? `
    <div class="dash-sec">最近发现</div>
    <div class="dash-findings">${findings.slice(-6).reverse().map(f => `
      <div class="find-card"><span class="fc-tag ${f.type || ""}">${esc(f.type || "")}</span>
        <span class="fc-txt">${esc(f.text || "")}</span></div>`).join("")}</div>` : ""}
    ${exps.length ? `
    <div class="dash-sec">经验沉淀 · 共 ${expTotal} 条（跨会话自我进化）</div>
    <div class="dash-findings">${exps.map(e => `
      <div class="find-card"><span class="fc-tag">${esc((e.tags || [])[0] || "经验")}</span>
        <span class="fc-txt">${esc(e.insight || "")}${e.context ? " —— " + esc(e.context) : ""}</span></div>`).join("")}</div>` : ""}`;
  box.querySelectorAll("[data-open]").forEach(row =>
    row.onclick = () => openSession(row.dataset.open));
}


function renderSessionHead() {
  const s = S.state.session;
  const running = !TERMINAL.has(s.status);
  $("#s-title").textContent = s.title || s.target || s.session_id;
  $("#s-title").title = s.target || "";
  $("#op-rename").onclick = () => startRename(S.cur, $("#s-title"));
  $("#s-meta").innerHTML = [
    `<span class="pill ${s.status}">${STATUS_CN[s.status] || s.status}</span>`,
    running && s.paused ? `<span class="pill paused">已暂停</span>` : "",
    `<span class="tag blue">${MODE_CN[s.mode] || s.mode}</span>`,
    `<button type="button" class="tag proj-chip" data-move-project title="项目分区 · 点击移动会话">${esc(projectName(s.project_id))}</button>`,
    // 用时扣除暂停与执行间隙区间（门内累计，实时随轮询刷新）
    s.started_at ? `<span class="tag">用时 ${fmtDur(s.started_at + (s.excluded_seconds || 0), s.finished_at)}</span>` : "",
    `<span class="tag mono">${esc(s.session_id)}</span>`,
  ].join("");
  const moveProject = $("#s-meta [data-move-project]");
  if (moveProject) moveProject.onclick = () => ProjectsPage.moveSession(S.cur);
  $("#s-err").innerHTML = [
    S.state?.recovery?.unresolved_count > 0
      ? `<div class="pause-banner">有 ${Number(S.state.recovery.unresolved_count)} 次操作的结果尚待核对。继续任务时，灵枢会收到这些记录；请先核实效果，避免重复操作。</div>` : "",
    running && s.paused
      ? `<div class="pause-banner">⏸ 已暂停——智能体将在当前步骤完成后停下；你仍可与灵枢对话，审批照常</div>` : "",
    s.error ? `<div class="err-banner">${esc(s.error)}</div>` : "",
  ].join("");
  $("#comp-send").disabled = false;
  $("#comp-attach").disabled = false;
  $("#comp-input").placeholder = running
    ? "给灵枢提意见、补充线索或上传附件…"
    : "继续对话——灵枢带着这段会话的完整上下文";
  $("#s-ops").innerHTML = `
    ${running ? `<button class="btn sm" id="op-pause">${s.paused ? "▶ 继续" : "⏸ 暂停"}</button>` : ""}
    ${running ? `<button class="btn danger sm" id="op-stop">停止</button>` : ""}
    <a class="btn sm" href="/api/sessions/${S.cur}/report/download" download>下载报告</a>
    <a class="btn sm" href="/api/sessions/${S.cur}/audit/download" download>导出审计</a>
    <button class="btn danger sm" id="op-del" ${running ? "disabled" : ""}>删除</button>`;
  if (running) $("#op-pause").onclick = async () => {
    const url = `/api/sessions/${S.cur}/${s.paused ? "resume" : "pause"}`;
    try { await api(url, { method: "POST", body: "{}" });
      toast(s.paused ? "已继续" : "已暂停——在途步骤跑完即停"); refreshState(); }
    catch (e) { toast(e.message, true); }
  };
  if (running) $("#op-stop").onclick = async () => {
    try { await api(`/api/sessions/${S.cur}/stop`, { method: "POST", body: "{}" });
      toast("已发送停止"); refreshState(); } catch (e) { toast(e.message, true); }
  };
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
  const rounds = (st.round_views || []).length;
  const bc = st.business_context || {};
  const coverage = { observed_transaction_flow: "交易流", public_flow: "公开流程",
                     entry_only: "仅入口", none: "无" }[bc.coverage];
  const phase = { queued: "等待调度", starting: "启动任务", mode_routing: "判断任务类型", intent_routing: "理解续聊意图",
    model_preflight: "检查模型", platform_profile: "解析平台档案",
    scope_confirmation: "确认测试范围",
    master_reply: "灵枢回复中", runtime_starting: "初始化工具与智能体",
    executing: "执行中", completed: "已完成", failed: "失败", stopped: "已停止",
    interrupted: "已中断" }[TERMINAL.has(st.session.status) ? st.session.status : st.session.phase] || st.session.phase || "等待启动";
  const firstChip = st.session.mode === "src" && coverage
    ? ["业务覆盖", coverage] : ["当前阶段", phase];
  $("#s-chips").innerHTML = [
    firstChip,
    ["收录漏洞", v.accepted || 0], ["候选/核验中", (v.candidate || 0) + (v.verifying || 0)],
    ["Flag", flagChipValue(st.flags)],
    ["决策轮次", rounds || "—"],
    ["Token", tokens != null ? Number(tokens).toLocaleString() : "—"],
  ].map(([k, x]) => `<div class="chip"><b>${x}</b>${k}</div>`).join("");
}

/* ================= 拓扑画布 ================= */
function topoModel() {
  const tp = (S.state && S.state.topology) || { dag_nodes: [], tasks: [] };
  const nodes = (tp.dag_nodes || []).map(n => ({...n})), tasks = tp.tasks || [];
  const byId = new Map(nodes.map(n => [n.node_id, n]));
  const depth = new Map();
  const depthOf = (n, seen = new Set()) => {
    if (depth.has(n.node_id)) return depth.get(n.node_id);
    if (seen.has(n.node_id)) return 0;
    seen.add(n.node_id);
    const deps = (n.depends_on || []).map(d => byId.get(d)).filter(Boolean);
    const d = deps.length ? 1 + Math.max(...deps.map(x => depthOf(x, seen))) : 0;
    depth.set(n.node_id, d); return d;
  };
  nodes.forEach(n => depthOf(n));
  const layers = new Map();
  nodes.forEach(n => {
    const d = depth.get(n.node_id) || 0;
    if (!layers.has(d)) layers.set(d, []);
    layers.get(d).push(n);
  });
  const assigned = new Set();
  const cardsOf = n => {
    const ids = new Set(n.task_ids || []);
    return tasks.filter(t => ids.has(t.task_id) || t.parent_dag_node === n.node_id)
      .map(t => { assigned.add(t.task_id); return t; });
  };
  const orphans = tasks.filter(t => !assigned.has(t.task_id) &&
    !nodes.some(n => (n.task_ids || []).includes(t.task_id) || t.parent_dag_node === n.node_id));
  return { nodes, layers, cardsOf, orphans };
}
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
  const vw = Math.max(box.clientWidth, 240), vh = Math.max(box.clientHeight, 240);
  const s = Math.min(vw / (w + 28), vh / (h + 28), 1.2);
  return { s, tx: (vw - w * s) / 2, ty: (vh - h * s) / 2 };
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

function drawTopo() {
  const box = $("#topo");
  if (!S.state) { box.innerHTML = ""; return; }
  const { nodes, layers, cardsOf, orphans } = topoModel();
  const running = !TERMINAL.has(S.state.session.status);
  // 督导台账（灵枢实时给子 Agent 的方向建议），任务卡据此挂提示芯片
  const ring = S.state.ring || { attempts: 0, steering: {} };
  const routeRow = r => {
    const pct = Math.round((r.progress || 0) * 100);
    const st = String(r.status || "").toLowerCase();
    const cls = st === "done" ? "done" : (st === "failed" || st === "pruned") ? "fail" : "";
    return `<div class="rt">
      <div class="rt-head"><span class="rt-dot ${esc(st)}"></span>
        <span class="rt-name">${esc(r.name || "")}</span>
        <span class="rt-pct">${pct}%</span></div>
      <div class="prog ${cls}"><i style="width:${pct}%"></i></div>
      ${r.note ? `<div class="rt-note">${esc(r.note)}</div>` : ""}
    </div>`;
  };
  const taskCard = t => {
    const tot = t.routes_total || 0, done = t.routes_done || 0;
    const pct = tot ? Math.round(done / tot * 100) : (t.status === "done" ? 100 : 0);
    const stCls = t.status === "done" ? "green" : t.status === "failed" ? "red" : "blue";
    const steer = (ring.steering || {})[t.agent_type];
    return `<div class="tcard" data-agent="${esc(t.agent_type || "")}">
      <div class="row1"><span class="who">${esc(agentName(t.agent_type))}</span>
        <span class="what" title="${esc(TASK_CN[t.task_type] || t.task_type || "")}">${esc(TASK_CN[t.task_type] || t.task_type || "")}</span>
        <span class="tag ${stCls} st">${esc(t.status || "")}</span></div>
      ${tot ? `<div class="prog ${done === tot ? "done" : ""}"><i style="width:${pct}%"></i></div>` : ""}
      <div class="steps">${tot ? `路线 ${done}/${tot} · ` : ""}步骤 ${t.steps || 0}</div>
      ${steer ? `<div class="steer-chip" title="${esc(steer.rationale || "")}">灵枢：${esc(steer.hint || "")}</div>` : ""}
    </div>`;
  };

  // ---- 渲染节点集合：DAG 节点按深度分层，孤儿任务挂首层伪节点
  const depths = [...layers.keys()].sort((a, b) => a - b);
  const layerIds = [], depthIndex = new Map();
  depths.forEach(d => {
    const arr = layers.get(d).map(n => n.node_id);
    arr.forEach(id => depthIndex.set(id, layerIds.length));
    layerIds.push(arr);
  });
  const ORPHAN = "__orphan__";
  if (orphans.length) {
    if (!layerIds.length) layerIds.push([]);
    layerIds[0].push(ORPHAN);
    depthIndex.set(ORPHAN, 0);
  }
  // 路由分支层：每个任务的路线作为子分支节点挂在其后（推理摘要并入分支）
  const taskById = new Map((S.state.topology.tasks || []).map(t => [t.task_id, t]));
  const routeMeta = new Map();
  const digestFor = agentType => {
    for (const [k, v] of S.think) {
      if (String(k).replace(/-[0-9a-f]{8,}$/i, "").replace(/-/g, "_") === agentType)
        return (v.tail || "").replace(/\s+/g, " ").slice(-150);
    }
    return "";
  };
  const routesAll = [];
  const routeSeen = new Map();
  nodes.filter(n => depthIndex.has(n.node_id)).forEach(n => {
    (n.task_ids || []).forEach(tid => {
      const t = taskById.get(tid);
      ((t && t.routes) || []).forEach(r => {
        const key = `${n.node_id}|${r.name || ""}`;
        const prev = routeSeen.get(key);
        if (prev) {
          prev.status = prev.status === "done" ? "done" : r.status;
          prev.progress = Math.max(prev.progress || 0, r.progress || 0);
          if (r.note) prev.note = r.note;
        } else {
          const item = { parent: n.node_id, agent: t.agent_type,
                         name: r.name, status: r.status,
                         progress: r.progress, note: r.note };
          routeSeen.set(key, item);
          routesAll.push(item);
        }
      });
    });
  });
  if (routesAll.length) {
    const layerIdx = layerIds.length;
    layerIds.push([]);
    routesAll.forEach((r, i) => {
      const id = `__route_${i}`;
      layerIds[layerIdx].push(id);
      depthIndex.set(id, layerIdx);
      routeMeta.set(id, r);
      nodes.push({ node_id: id, title: r.name || "路线",
                   tactic: `${Math.round((r.progress || 0) * 100)}%`,
                   status: r.status === "running" ? "running" : r.status,
                   depends_on: [] });
    });
  }

  // 发现组：黑板发现台账直投——发现即节点、产出即连线（拓扑=黑板架构的
  // 直观投影）。节点随进度自动扩充，连线是真实出处：写下它的路线分支，
  // 路线不在当前图上（旧轮）时挂该轮 DAG 方向节点；不凭空造边
  const findings = (S.state && S.state.findings) || [];
  // 展示上限：防画布刷屏；被折叠的数量写进图例，台账仍在发现组详情
  const uniqAssets = [...new Map(
    findings.filter(f => f.type === "asset")
            .map(f => [String(f.url || f.text || ""), f])).values()];
  const dispFindings = [
    ...uniqAssets.slice(-12),
    ...findings.filter(f => f.type === "note").slice(-10),
    ...findings.filter(f => f.type === "flag").slice(-4),
  ].sort((a, b) => (a.ts || 0) - (b.ts || 0));
  const foldedFindings = Math.max(0, findings.length - dispFindings.length);
  const producerOf = f => {
    if (f.agent && f.route) {
      const idx = routesAll.findIndex(
        r => r.agent === f.agent && r.name === f.route);
      if (idx >= 0 && depthIndex.has(`__route_${idx}`)) return `__route_${idx}`;
    }
    if (f.round != null) {
      const n = nodes.find(x => x.round_created === f.round
                                && depthIndex.has(x.node_id)
                                && !String(x.node_id).startsWith("__"));
      if (n) return n.node_id;
    }
    return "";
  };
  const FNODE = i => `__finding_${i}`;
  if (dispFindings.length) {
    const layerIdx = layerIds.length;
    layerIds.push([]);
    dispFindings.forEach((f, i) => {
      const id = FNODE(i);
      layerIds[layerIdx].push(id);
      depthIndex.set(id, layerIdx);
      const kind = ({ note: "笔记", flag: "Flag", asset: "入口" })[f.type]
        || f.type || "发现";
      nodes.push({ node_id: id,
                   title: String(f.url || f.text || "").slice(0, 90),
                   tactic: f.round ? `${kind} · R${f.round}` : kind,
                   status: f.type === "flag" ? "done" : "running",
                   depends_on: [] });
    });
  }
  const ids = [...depthIndex.keys()];
  if (!ids.length) {
    box.innerHTML = `<div class="empty-note">等待灵枢规划——任务拓扑将在此实时展开</div>`;
    renderApprovals();
    renderQuestions();
    return;
  }
  const nodeOf = new Map(nodes.map(n => [n.node_id, n]));
  const preds = new Map(ids.map(id => [id, []])), succs = new Map(ids.map(id => [id, []]));
  for (const n of nodes) {
    if (!depthIndex.has(n.node_id)) continue;
    for (const dep of (n.depends_on || [])) {
      if (!depthIndex.has(dep)) continue;
      preds.get(n.node_id).push(dep);
      succs.get(dep).push(n.node_id);
    }
  }
  // 路由分支连线：路线节点 ← 产生它的任务节点
  routesAll.forEach((r, i) => {
    const id = `__route_${i}`;
    if (depthIndex.has(r.parent)) {
      preds.get(id).push(r.parent);
      succs.get(r.parent).push(id);
    }
  });
  // 发现连线：产出者（路线分支/该轮 DAG 节点）→ 其写下的发现；无出处
  // 信息（旧记录/SRC 发现）时退回前一列锚点。连线即真实出处
  let findingAnchor = "";
  if (dispFindings.length) {
    const prevLayer = (layerIds[depthIndex.get(FNODE(0)) - 1] || [])
      .filter(id => id !== ORPHAN);
    findingAnchor = prevLayer[prevLayer.length - 1] || prevLayer[0] || "";
    dispFindings.forEach((f, i) => {
      const src = producerOf(f) || findingAnchor;
      if (src && depthIndex.has(src)) {
        preds.get(FNODE(i)).push(src);
        succs.get(src).push(FNODE(i));
      }
    });
  }
  topoReorder(layerIds, preds, succs);

  const expanded = S.topoExpand || {};
  const cardHtml = n => {
    if (String(n.node_id).startsWith("__route_")) {
      const r = routeMeta.get(n.node_id) || {};
      const pct = Math.round((r.progress || 0) * 100);
      const st = nodeStatus(r.status);
      const digest = digestFor(r.agent);
      const isExp = expanded[n.node_id];
      return `<div class="tnode bnode" data-node="${esc(n.node_id)}" style="visibility:hidden">
        <div class="tact"><span class="dot ${st}"></span><b title="${esc(r.name || "")}">${esc(agentName(r.agent))} · ${esc(r.name || "")}</b>
          <span class="tac">${pct}%</span></div>
        <div class="prog ${pct === 100 ? "done" : ""}"><i style="width:${pct}%"></i></div>
        ${r.note ? `<div class="rt-note">${esc(isExp ? r.note : (r.note || "").slice(0, 120))}</div>` : ""}
        ${digest ? `<div class="mind-digest">✦ ${esc(digest)}</div>` : ""}
      </div>`;
    }
    const st = nodeStatus(n.status);
    return `<div class="tnode ${st === "active" ? "active" : ""}" data-node="${esc(n.node_id)}"
      style="visibility:hidden">
      <div class="tact"><span class="dot ${st}"></span><b title="${esc(n.title || n.tactic)}">${esc(n.title || n.tactic)}</b>
        <span class="tac" title="${esc(n.tactic || "")}">${esc(n.tactic || "")}</span></div>
      <div class="tcards">${cardsOf(n).map(taskCard).join("")}</div></div>`;
  };
  const orphanCard = `<div class="tnode" data-node="${ORPHAN}" style="visibility:hidden">
      <div class="tact"><span class="dot ${running ? "active" : "done"}"></span><b>下发任务</b></div>
      <div class="tcards">${orphans.map(taskCard).join("")}</div></div>`;
  const steerItems = Object.entries(ring.steering || {});
  const opinions = (S.state && S.state.user_opinions) || [];
  const latestOp = opinions.length ? opinions[opinions.length - 1] : null;
  const replies = (S.state && S.state.master_replies) || [];
  const latestReply = replies.length ? replies[replies.length - 1] : null;
  const unodeHtml = (latestOp || latestReply) ? `<div class="unode" id="topo-unode" style="visibility:hidden">
      <b>你<span class="utag">意见 ${opinions.length}</span></b>
      ${latestOp ? `<div class="txt">${esc(latestOp.text || "")}</div>` : ""}
      ${latestReply ? `<div class="reply-txt" title="${esc(latestReply.text || "")}">灵枢：${esc(latestReply.text || "")}</div>` : ""}
      <small>消息直达灵枢并转达执行中的智能体</small></div>` : "";

  // 节点详情的数据源随图一并留存（点击路线/发现/孤儿节点时取用）
  S.topoRouteMeta = routeMeta;
  S.topoFindings = dispFindings;
  S.topoOrphans = orphans;
  box.innerHTML = `<div id="topo-stage">
      <svg class="edges"></svg>
      ${unodeHtml}
      <div class="hub ${running ? "live" : ""}" id="topo-hub" style="visibility:hidden"><b>灵枢</b><small>主 Agent · 集中决策</small></div>
      ${ids.filter(id => id !== ORPHAN).map(id => cardHtml(nodeOf.get(id))).join("")}
      ${orphans.length ? orphanCard : ""}
    </div>
    <div class="topo-tools">
      <button id="topo-zin" title="放大">＋</button>
      <button id="topo-zout" title="缩小">－</button>
      <button id="topo-fit" title="适应窗口">⤢</button>
    </div>
    <div class="topo-hint">滚轮缩放 · 拖拽平移 · 双击复位 · 点击节点看详情${foldedFindings > 0 ? ` · 黑板 ${findings.length} 条发现（展示最近 ${dispFindings.length} 条）` : ""}</div>
    <div class="topo-detail" id="topo-detail" hidden>
      <div class="td-head"><b id="td-title"></b>
        <button class="td-close" id="td-close">×</button></div>
      <div class="td-body" id="td-body"></div>
    </div>`;
  renderApprovals();
  renderQuestions();

  // ---- 先隐藏渲染量高，再按层堆叠定位（节点高度随路线卡片数变化）
  const stage = box.querySelector("#topo-stage");
  const hubEl = box.querySelector("#topo-hub");
  const unodeEl = box.querySelector("#topo-unode");
  const elOf = new Map(ids.map(id =>
    [id, stage.querySelector(`[data-node="${CSS.escape(id)}"]`)]));
  const heights = new Map(ids.map(id => [id, elOf.get(id).offsetHeight]));
  const hubH = hubEl.offsetHeight, hubW = hubEl.offsetWidth;
  const unW = unodeEl ? unodeEl.offsetWidth : 0;
  const unH = unodeEl ? unodeEl.offsetHeight : 0;
  const unX = TOPO_GEO.PAD;
  const hubXpos = unX + unW + (unodeEl ? 26 : 0);   // 意见节点占灵枢左侧
  const layerH = layerIds.map(arr =>
    arr.reduce((a, id) => a + heights.get(id), 0) + TOPO_GEO.GAP_Y * Math.max(arr.length - 1, 0));
  const totalH = Math.max(...layerH);
  const centerY = TOPO_GEO.PAD + totalH / 2;
  const xOf = new Map(), yOf = new Map();
  let cx = hubXpos + hubW + TOPO_GEO.HUB_GAP;
  layerIds.forEach((arr, i) => {
    let y = TOPO_GEO.PAD + (totalH - layerH[i]) / 2;
    for (const id of arr) {
      xOf.set(id, cx);
      yOf.set(id, y);
      y += heights.get(id) + TOPO_GEO.GAP_Y;
    }
    cx += TOPO_GEO.NODE_W + TOPO_GEO.GAP_X;
  });
  const stageW = cx - TOPO_GEO.GAP_X + TOPO_GEO.PAD;
  const stageH = totalH + TOPO_GEO.PAD * 2;
  for (const id of ids) {
    const el = elOf.get(id);
    el.style.transform = `translate(${xOf.get(id)}px, ${yOf.get(id)}px)`;
    el.style.visibility = "";
  }
  hubEl.style.transform =
    `translate(${hubXpos}px, ${centerY - hubH / 2}px)`;
  if (unodeEl) {
    unodeEl.style.transform = `translate(${unX}px, ${centerY - unH / 2}px)`;
    unodeEl.style.visibility = "";
  }
  hubEl.style.visibility = "";
  stage.style.width = stageW + "px";
  stage.style.height = stageH + "px";

  // ---- 连线：源右缘 → 目标左缘，水平三次贝塞尔（缩放随画布同步）
  const svg = stage.querySelector("svg.edges");
  svg.setAttribute("width", stageW);
  svg.setAttribute("height", stageH);
  svg.setAttribute("viewBox", `0 0 ${stageW} ${stageH}`);
  const hubX = hubXpos + hubW, hubY = centerY;
  const link = (x1, y1, x2, y2, cls, title = "") => {
    const dx = Math.max(30, (x2 - x1) * 0.42);
    return `<path class="${cls}" d="M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}">${title ? `<title>${esc(title)}</title>` : ""}</path>`;
  };
  const midY = id => yOf.get(id) + heights.get(id) / 2;
  let paths = "";
  if (unodeEl) {
    // 你 → 灵枢：意见流入决策环
    paths += link(unX + unW, hubY, hubXpos, hubY, "user-edge");
  }
  for (const n of nodes) {
    const id = n.node_id;
    if (!xOf.has(id)) continue;
    const st = nodeStatus(n.status);
    const deps = preds.get(id) || [];
    if (!deps.length) paths += link(hubX, hubY, xOf.get(id), midY(id), st);
    for (const d of deps) {
      paths += link(xOf.get(d) + TOPO_GEO.NODE_W, midY(d), xOf.get(id), midY(id), st);
    }
  }
  if (orphans.length) {
    paths += link(hubX, hubY, xOf.get(ORPHAN), midY(ORPHAN), running ? "active" : "done");
  }
  // 督导连线：灵枢 → 建议所指向的角色任务节点（悬停看完整建议）
  for (const [agent, v] of steerItems) {
    const el = stage.querySelector(`.tcard[data-agent="${CSS.escape(agent)}"]`);
    const tn = el && el.closest("[data-node]");
    const nid = tn && tn.dataset.node;
    if (nid && xOf.has(nid)) {
      paths += link(hubX, hubY, xOf.get(nid), midY(nid), "steer-edge",
                    `灵枢 → ${agentName(agent)}：${v.hint || ""}`);
    }
  }
  // 积累链：同一产出者的连续发现按时间上下相连——先探到的节点支撑
  // 后续发现，黑板节点互相提供数据支撑的可视形态（同列短竖线，散开不画）
  const srcOf = f => producerOf(f) || findingAnchor;
  for (let i = 1; i < dispFindings.length; i++) {
    const prev = dispFindings[i - 1];
    if (!srcOf(prev) || srcOf(prev) !== srcOf(dispFindings[i])) continue;
    const ia = FNODE(i - 1), ib = FNODE(i);
    if (!xOf.has(ia) || !xOf.has(ib) || xOf.get(ia) !== xOf.get(ib)) continue;
    const y1 = yOf.get(ia) + heights.get(ia), y2 = yOf.get(ib);
    if (y2 > y1 && y2 - y1 < 140) {
      const cx = xOf.get(ia) + TOPO_GEO.NODE_W / 2;
      paths += `<path class="chain-edge" d="M ${cx} ${y1} C ${cx} ${y1 + 20}, ${cx} ${y2 - 20}, ${cx} ${y2}"></path>`;
    }
  }
  svg.innerHTML = paths;

  // ---- 视图：首次进入自动适应，此后保留用户平移/缩放
  if (!S.topoView) S.topoView = topoFit(box, stageW, stageH);
  topoApplyView(box, S.topoView);
  box.querySelector("#topo-zin").onclick = () => topoZoom(box, 1.25);
  box.querySelector("#topo-zout").onclick = () => topoZoom(box, 0.8);
  box.querySelector("#topo-fit").onclick = () => {
    S.topoView = topoFit(box, stageW, stageH);
    topoApplyView(box, S.topoView);
  };
}

/* 画布手势：滚轮以指针为中心缩放、按住拖拽平移、双击复位（绑定一次） */
(function bindTopoGestures() {
  const box = $("#topo");
  box.addEventListener("wheel", (ev) => {
    if (!S.topoView || S.tab !== "topo") return;
    ev.preventDefault();
    const r = box.getBoundingClientRect();
    const mx = ev.clientX - r.left, my = ev.clientY - r.top, v = S.topoView;
    const ns = Math.min(2.4, Math.max(0.3, v.s * Math.exp(-ev.deltaY * 0.0015)));
    v.tx = mx - (mx - v.tx) * (ns / v.s);
    v.ty = my - (my - v.ty) * (ns / v.s);
    v.s = ns;
    topoApplyView(box, v);
  }, { passive: false });
  let drag = null;
  let downPos = null;
  let downTarget = null;
  box.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0 || !S.topoView || ev.target.closest("button, input, textarea, select, a")) return;
    ev.preventDefault();
    downPos = { x: ev.clientX, y: ev.clientY };
    downTarget = ev.target.closest("[data-node]");
    drag = { x: ev.clientX, y: ev.clientY, tx: S.topoView.tx, ty: S.topoView.ty };
    box.setPointerCapture(ev.pointerId);
    box.classList.add("grabbing");
  });
  box.addEventListener("pointermove", (ev) => {
    if (!drag) return;
    S.topoView.tx = drag.tx + ev.clientX - drag.x;
    S.topoView.ty = drag.ty + ev.clientY - drag.y;
    topoApplyView(box, S.topoView);
  });
  const stop = (ev) => {
    if (downPos && ev && ev.type === "pointerup") {
      const dx = Math.abs(ev.clientX - downPos.x);
      const dy = Math.abs(ev.clientY - downPos.y);
      if (dx < 6 && dy < 6 && downTarget) {
        const id = downTarget.dataset.node;
        if (id) showNodeDetail(id, downTarget);
      }
    }
    drag = null; downPos = null; downTarget = null;
    box.classList.remove("grabbing");
  };
  box.addEventListener("pointerup", stop);
  box.addEventListener("pointercancel", stop);
  box.addEventListener("dblclick", (ev) => {
    if (ev.target.closest(".topo-tools")) return;
    S.topoView = null;
    drawTopo();
  });
})();
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
  const panel = $("#topo-detail");
  if (!panel) return;
  if (panel.hidden === false && panel._curId === nodeId) {
    panel.hidden = true; panel._curId = null; return;
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
  if (nodeId.startsWith("__route_")) {
    // 路线分支节点：执行现场全量呈现（agent / 状态 / 进度 / 最新笔记）
    const r = (S.topoRouteMeta || new Map()).get(nodeId);
    if (r) {
      title = `${agentName(r.agent)} · ${r.name || "路线"}`;
      const pct = Math.round((r.progress || 0) * 100);
      body = `<div class="td-task">
        <div class="td-route-h"><b>${esc(r.status || "")}</b><span class="mono">${pct}%</span></div>
        <div class="prog ${pct === 100 ? "done" : ""}"><i style="width:${pct}%"></i></div>
        ${r.note ? `<div class="td-note">${esc(r.note)}</div>`
                : '<div class="td-empty">路线尚无现场笔记</div>'}
      </div>`;
    }
  } else if (nodeId.startsWith("__finding_")) {
    // 发现节点：完整内容与出处（哪条路线/哪一轮写下——黑板记录直读）
    const f = (S.topoFindings || [])[Number(nodeId.slice(10))];
    if (f) {
      title = ({ note: "共享笔记", flag: "Flag", asset: "入口 / 资产" })[f.type] || "发现";
      const who = [f.agent && agentName(f.agent), f.route,
                   f.round != null ? `第 ${f.round} 轮` : ""].filter(Boolean).join(" · ");
      body = `<div class="td-task">
        ${who ? `<div class="td-agent">出处：${esc(who)}</div>` : ""}
        ${f.url ? `<div class="td-note" style="word-break:break-all">${esc(f.url)}</div>` : ""}
        <div class="td-note" style="white-space:pre-wrap">${esc(f.text || "")}</div></div>`;
    }
  } else if (nodeId === "__orphan__") {
    title = "下发任务";
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
                        : '<div class="td-empty">暂无任务数据</div>';
  }
  $("#td-title").textContent = title;
  $("#td-body").innerHTML = body;
  panel.querySelector("#td-close").onclick = () => { panel.hidden = true; };
}

let vulnPageSession = null, vulnOffset = 0, vulnRows = null, vulnLoading = false, vulnGeneration = 0;
async function loadVulnPage(offset) {
  const session = S.cur, version = ++vulnGeneration;
  vulnLoading = true; renderVulns();
  try {
    const page = await api(`/api/sessions/${encodeURIComponent(session)}/vulnerabilities?offset=${offset}&limit=50`);
    if (S.cur !== session || version !== vulnGeneration) return;
    vulnOffset = offset; vulnRows = page.items;
  } catch (error) { if (S.cur === session) toast(`漏洞读取失败：${error.message}`, true); }
  finally { if (S.cur === session && version === vulnGeneration) {vulnLoading = false;renderVulns();} }
}
function renderVulns() {
  if (vulnPageSession !== S.cur) {vulnPageSession=S.cur;vulnOffset=0;vulnRows=null;vulnLoading=false;vulnGeneration++;}
  const vs = vulnOffset ? (vulnRows || []) : (S.state.vulns.items || []);
  $("#tab-vulns").innerHTML = vs.length ? `<div class="grid-cards">${vs.map(v => {
    const [sevCn, sevCls] = SEV_CN[v.severity] || [v.severity || "未定级", "blue"];
    return `<div class="card vcard">
      <div class="h"><b>${esc(v.title || v.vuln_type || "漏洞")}</b>
        <span class="tag ${sevCls}">${sevCn}</span>
        <span class="tag ${v.status === "accepted" ? "green" : ""}">${esc(({candidate:"待独立复测",verifying:"鉴真复测中",inconclusive:"证据不足 · 未收录",accepted:"确认收录",rejected_false_positive:"误报 · 不收录",rejected_compliance:"不符合规则 · 不收录"})[v.status] || v.status || "")}</span>
        ${v.poc_verified ? `<span class="tag gold">PoC 已验证</span>` : ""}</div>
      ${v.endpoint ? `<div class="ep">${esc(v.endpoint)}</div>` : ""}
      ${v.verification?.reason ? `<p>${esc(v.verification.reason)}</p>` : ""}
      ${v.verification?.rule_quote ? `<p class="muted">危害规则：${esc(v.verification.rule_quote)}</p>` : ""}
      <div class="muted" style="font-size:11.5px">${esc(v.vuln_type || "")}${v.cvss_score != null ? " · CVSS " + v.cvss_score : ""} · ${esc(v.vuln_id || "")}</div>
    </div>`; }).join("")}</div>`
    : `<div class="empty-note">尚无漏洞记录</div>`;
  const total = S.state.vulns.total ?? vs.length;
  if (total > 50 || vulnOffset) {
    $("#tab-vulns").insertAdjacentHTML("afterbegin", `<div class="vuln-pagination"><span>共 ${total} 条 · 当前 ${vulnOffset+1}–${vulnOffset+vs.length}</span><button class="btn sm" data-vuln-prev ${vulnLoading || !vulnOffset ? "disabled" : ""}>上一页</button><button class="btn sm" data-vuln-next ${vulnLoading || vulnOffset+vs.length>=total ? "disabled" : ""}>下一页</button><button class="btn sm" data-vuln-refresh ${vulnLoading ? "disabled" : ""}>${vulnLoading ? "读取中…" : "刷新当前页"}</button></div>`);
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
        ${x.accepted ? "已判定" : x.submitted ? "已提交" : "已提取"}</span>
        <span class="muted" style="font-size:11.5px"> ${esc(x.challenge_id || "")}</span></div>
    </div>`).join("")}</div>`
    : `<div class="empty-note">尚无 Flag 记录</div>`;
}

/* ---- 解题轨迹：按真实发生顺序还原成一条可读时间线 ---- */
let traceStick = true;
const TRACE_AGENT_SLOTS = new Map();
const TRACE_TURNS = new Map();
function traceReset() {
  if (traceScrollFrame !== null) cancelAnimationFrame(traceScrollFrame);
  traceScrollFrame = null; traceForceScroll = false;
  SessionObserver.reset();
  AgentWorkbench.reset();
  $("#trace-box").innerHTML = '<div class="empty-note">等待灵枢开工——过程将按真实时间顺序在此展开</div>';
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
  const box = $("#trace-box");
  box.querySelector(".empty-note")?.remove();
  box.insertAdjacentHTML("beforeend", traceRow(e, body, cls));
  traceScroll();
  const row = box.lastElementChild;
  if (row.classList.contains("tl-turn")) TRACE_TURNS.set(String(e.actor || ""), row);
  row.dataset.eventTs = e.ts || "";
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
    if (summary) summary.textContent = "思考过程";
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
  if (args._redacted) return "正在读取参数";
  const one = v => String(v ?? "").replace(/\s+/g, " ").trim();
  if (name === "http_request") {
    let target = one(args.path || args.url || "/");
    try { if (/^https?:\/\//i.test(target)) target = new URL(target).pathname + new URL(target).search; } catch {}
    return `${one(args.method || "GET").toUpperCase()} ${target}`.slice(0, 180);
  }
  if (name === "shell") return one(args.command).slice(0, 180);
  if (name === "python") return one(args.code).split("\n")[0].slice(0, 180) || "运行脚本";
  if (name === "note") return one(args.text).slice(0, 180);
  if (name === "analyze_text") return "分析题面文本";
  if (name === "analyze_attachments") return "分析全部附件";
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
  return compact === "{}" ? "执行" : compact.slice(0, 180);
}
/* 工具足迹条：回合尾逐行列出模型实际使用的工具与命令原文 */
const TOOLS_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>';
function toolFootprint(name, args) {
  args = args && typeof args === "object" ? args : {};
  if (args._redacted) return "参数见调用卡片";
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
    flow.insertAdjacentHTML("beforeend", `<div class="tl-tools"><div class="th">${TOOLS_SVG}
      <span>已使用 <b class="n">0</b> 个工具</span></div><div class="lines"></div></div>`);
    strip = flow.lastElementChild;
  }
  flow.append(strip);   // 始终钉在回合流末尾，后续动作卡插在其前
  return strip;
}
function tracePush(e) {
  const box = $("#trace-box");
  if (!box) return;
  const d = (typeof e.detail === "object" && e.detail) || {};
  const actor = String(e.actor || "");
  SessionObserver.push(e);
  AgentWorkbench.push(e);
  switch (e.type) {
    case "task.start":
      traceAppend(e, `<div class="m">开始执行 · ${esc(actorName(actor))}</div>`, "tl-sys");
      break;
    case "agent.turn": {
      traceFinishThinking(traceTurn(actor));
      const turn = traceAppend(e, `<div class="hd"><span class="who">${esc(actorName(actor))}</span>
        <span class="k">第 ${esc(d.turn || "?")} 回合${d.forced_finish ? " · 正在收尾" : ""}</span></div>
        ${d.turn === 1 && d.angle ? `<div class="ang">${esc(d.angle)}</div>` : ""}<div class="flow"></div>`, "tl-turn");
      turn.dataset.turn = String(d.turn || "");
      break;
    }
    case "agent.delta": {
      const kind = d.kind || "text";
      if (kind === "tool_call") break;
      const turn = traceTurn(actor) || traceAppend(e,
        `<div class="hd"><span class="who">${esc(actorName(actor))}</span><span class="k">本页回合片段</span></div><div class="flow"></div>`, "tl-turn");
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
            '<div class="tl-thinkchip" title="在上方子 Agent 工作台查看实时分析"></div>');
          chip = flow.lastElementChild;
        }
        chip.textContent = `✦ 推理 ${fmtK(rec.chars)} 字`;
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
      el.textContent += text;
      traceScroll();
      break;
    }
    case "agent.tool_call": {
      const turn = traceTurn(actor) || traceAppend(e,
        `<div class="hd"><span class="who">${esc(actorName(actor))}</span><span class="k">工具执行</span></div><div class="flow"></div>`, "tl-turn");
      traceFinishThinking(turn);
      const flow = turn.querySelector(".flow");
      for (const c of (d.calls || [])) {
        flow.insertAdjacentHTML("beforeend", `<div class="tl-act" data-id="${esc(c.id || "")}">
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
        traceAppend(e, `<details><summary>${esc(SessionObserver.toolName(d.name || "工具"))} · ${d.ok ? "已返回" : "失败"}（调用见更早记录）</summary><pre>${esc(SessionObserver.cleanText(d.result || d.note || "未记录原始响应"))}</pre></details>`, "tl-sys");
        break;
      }
      const st = el.querySelector(".st");
      st.textContent = d.ok ? "已返回" : "失败";
      st.className = "st " + (d.ok ? "y" : "n");
      el.querySelector(".nt").textContent = SessionObserver.resultSummary(d);
      el.querySelector(".tl-output").textContent = SessionObserver.cleanText(d.result || d.note || "未记录原始响应");
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
      traceAppend(e, `<div class="m">捕获 Flag · ${esc(d.value_preview || "")}</div>`, "tl-flag");
      break;
    case "model.started":
      traceAppend(e, `<div class="m">${esc(actorName(actor))} · 正在调用 ${esc(d.model || "模型")} · ${esc(SessionObserver.toolName(d.task_type || "任务决策"))}</div>`, "tl-sys");
      break;
    case "model.call": {
      // 调用收尾即定格 Markdown：文本流已结束，不等下一次回合边界
      // （否则纯推理续轮/会话收尾前的流式正文会一直保持原文）
      traceFinishThinking(traceTurn(actor));
      const n = SessionObserver.usageFor(e, S.state);
      const row = traceAppend(e, `<div class="m"><strong>${esc(d.model || "模型")} 已返回</strong> · <span class="model-tokens">${n == null ? "单次用量未记录" : `${Number(n).toLocaleString("zh-CN")} token`}</span> · ${(Number(d.latency_ms || 0) / 1000).toFixed(1)} 秒${d.task_type ? ` · ${esc(d.task_type)}` : ""}</div>`, "tl-sys tl-model");
      row.dataset.callTs = e.ts || "";
      break;
    }
    case "model.failed":
    case "task.failed":
    case "llm.step_fallback":
      traceFinishThinking(traceTurn(actor));   // 调用异常收尾：已流入的正文同样要定格
      traceAppend(e, `<div class="tl-err">${esc(actorName(actor))} · ${esc(d.error || "执行未成功，等待重新决策")}</div>`, "tl-sys");
      break;
    case "model.cancelled":
      traceFinishThinking(traceTurn(actor));
      traceAppend(e, `<div class="m">${esc(actorName(actor))} · 模型调用已取消，本次用量未返回</div>`, "tl-sys");
      break;
    case "task.cancelled":
    case "task.pruned":
      traceAppend(e, `<div class="m">${esc(actorName(actor))} · ${e.type === "task.cancelled" ? "任务已撤销" : "路线已剪枝"}</div>`, "tl-sys");
      break;
    case "task.done":
      traceFinishThinking(traceTurn(actor));
      traceAppend(e, `<div class="m">${esc(actorName(actor))} 已完成任务</div>`, "tl-sys");
      break;
    case "user.message":
      traceAppend(e, `<div class="bubble"><div class="btxt">${esc(d.content || "")}</div></div>`, "tl-user");
      break;
    case "system.lifecycle":
      if (d.action === "session_kickoff")
        traceAppend(e, `<div class="m">灵枢开工准备中——加载任务配置…</div>`, "tl-sys");
      break;
    case "engagement.paused":
      traceAppend(e, `<div class="m">⏸ 已暂停——在途步骤跑完即停，对话与审批照常</div>`, "tl-sys");
      break;
    case "engagement.resumed":
      traceAppend(e, `<div class="m">▶ 已继续${d.paused_seconds ? `（暂停 ${fmtElapsed(d.paused_seconds)}，不计入用时）` : ""}</div>`, "tl-sys");
      break;
    case "system.gap":
      // 执行间隙（合盖待机/Ctrl+Z/VM 快照）：时间线如实标记，区间不计入用时
      traceAppend(e, `<div class="m">💤 检测到执行间隙 ${fmtElapsed(d.gap_seconds || 0)}——控制台刚刚未在运行，该区间不计入用时</div>`, "tl-sys");
      break;
    case "user.attachment": {
      const sz = fmtSize(d.size);
      traceAppend(e, `<div class="bubble"><div class="btxt">📎 ${esc(d.name || "")}${sz ? ` <span class="k">${esc(sz)}</span>` : ""}</div>
        ${d.path ? `<div class="bpath">${esc(d.path)}</div>` : ""}</div>`, "tl-user");
      break;
    }
    case "verification.queued":
      traceAppend(e, `<div class="m">候选入口已交给鉴真独立复测 · ${esc(d.vuln_id)}</div>`, "tl-sys");
      break;
    case "verification.started":
      traceAppend(e, `<div class="hd"><span class="who">鉴真</span><span class="k">独立危害复测</span></div><p>${esc(d.title || d.vuln_id)}</p>`, "tl-master");
      break;
    case "verification.result":
      traceAppend(e, `<details class="secretary-card" open><summary>鉴真 · ${esc(({confirmed:"确认危害，收录",false_positive:"误报，不收录",out_of_rules:"不符合规则，不收录",inconclusive:"证据不足，未收录"})[d.verdict] || "复测完成")}</summary><p>${esc(d.title || "")}</p><p>${esc(d.reason || "")}</p>${d.rule_quote ? `<p>用户危害规则：${esc(d.rule_quote)}</p>` : ""}${[...(d.evidence || []),...(d.controls || [])].map(ref=>`<p class="mono">${esc(ref.execution_id)} · ${esc(ref.quote)}</p>`).join("")}</details>`, "tl-master");
      break;
    case "verification.error":
      traceAppend(e, `<div class="m">独立复测未完成，不收录：${esc(d.error || "原因未提供")}</div>`, "tl-sys");
      break;
    case "secretary.suggested":
      traceAppend(e, `<details class="secretary-card"><summary>秘书 · ${(d.suggestions || []).length} 条参谋建议</summary>${(d.suggestions || []).map(item => `<article><strong>${esc(item.summary)}</strong><p>${esc(item.benefit)}</p><small>不确定性：${esc(item.uncertainty || "未说明")}</small><p class="mono">${esc((item.evidence_refs || []).join(" · "))}</p></article>`).join("") || '<p>本次没有需要补充的建议。</p>'}</details>`, "tl-master");
      break;
    case "secretary.decided":
      traceAppend(e, `<details class="secretary-card"><summary>灵枢 · 已评估秘书建议</summary>${(d.decisions || []).map(item => `<article><strong>${esc(({accepted:"采纳",partial:"部分采纳",rejected:"拒绝",needs_evidence:"待补证"})[item.verdict] || "待评估")}</strong><p>${esc((d.suggestions || []).find(x=>x.id===item.id)?.summary || item.id)}</p><p>${esc(item.reason)}</p></article>`).join("")}</details>`, "tl-master");
      break;
    case "secretary.failed":
      traceAppend(e, '<div class="m">秘书咨询未完成，灵枢沿用原决策继续。</div>', "tl-sys");
      break;
    case "ring.steer":
      traceAppend(e, `<div class="hd"><span class="who">灵枢</span>
        <span class="k">→ ${esc(agentName(d.agent))}</span></div>
        <div class="tl-reply">${esc(d.hint || "")}</div>`, "tl-master");
      break;
    case "decision.change": {
      if (d.action === "llm_mode_route") {
        const verdict = (d.intent === "dialogue" || d.intent === "rules")
          ? (d.intent === "dialogue" ? "对话" : "规则")
          : (MODE_CN[d.llm_mode] || d.llm_mode || "?");
        traceAppend(e, `<div class="hd"><span class="who">灵枢</span>
          <span class="k">任务模式判定</span></div>
          <div class="ang">→ ${esc(verdict)}${d.rationale ? ` · ${esc(d.rationale)}` : ""}</div>`, "tl-master");
      } else if (d.action === "dispatch_plan" || d.action === "dispatch_plan_skipped") {
        traceAppend(e, `<div class="m">灵枢已定下发计划（任务复杂度 · 路线预算 · 思考强度）</div>`, "tl-sys");
      } else if (d.action === "meta_plan") {
        const n = Array.isArray(d.new_nodes) ? d.new_nodes.length : 0;
        traceAppend(e, `<div class="hd"><span class="who">${esc(actorName(actor))}</span>
          <span class="k">第 ${esc(d.round ?? "?")} 轮规划${n ? ` · ${n} 个方向节点` : ""}</span></div>
          ${d.analysis ? `<div class="ang">${esc(String(d.analysis))}</div>` : ""}`, "tl-master");
      } else if (d.action === "review") {
        // 复盘结论优先显示灵枢自己的话（用户追问的答案也走这里）
        const body = String(d.llm_reason || "").trim()
          || (d.continue ? "继续迭代" : `收敛：${d.stop_reason || "目标达成"}`);
        traceAppend(e, `<div class="hd"><span class="who">${esc(actorName(actor))}</span>
          <span class="k">第 ${esc(d.round ?? "?")} 轮复盘</span></div>
          <div class="ang md">${mdRender(body)}</div>`, "tl-master");
      } else if (d.action === "user_feed_consumed") {
        const cn = { steering: "派发督导", review: "轮末复盘" }[d.channel] || "决策";
        traceAppend(e, `<div class="tl-reply">已收到你的意见，纳入本轮${cn}。</div>`, "tl-master");
      }
      break;
    }
    case "master.reply":
      // 灵枢↔用户直达子会话的左气泡：名章为身份（无文字标签订），与用户右气泡成对
      traceAppend(e, `<i class="seal" aria-label="灵枢">灵</i><div class="bubble">
        <div class="btxt md">${mdRender(d.text || "")}</div></div>`, "tl-master tl-conv");
      break;
    case "master.reply_failed":
      // 应答通道失败必须可见——否则用户消息像被吞掉
      traceAppend(e, `<i class="seal" aria-label="灵枢">灵</i><div class="bubble">
        <div class="btxt">应答暂时失败（${esc(d.error || "未知原因")}）——你的消息已留存，不会丢失</div></div>`, "tl-master tl-conv");
      break;
    case "agent.spawn":
      traceAppend(e, `<div class="m">🔮 派生分支 · ${esc(d.child || "")}</div>`, "tl-sys");
      break;
    case "agent.spawn_done":
      traceAppend(e, `<div class="m">🔮 分支 ${esc(d.child || "")} 收尾（Flag ${d.flags || 0}）</div>`, "tl-sys");
      break;
    case "agent.ask_user":
      traceAppend(e, `<div class="tl-ask">向你提问 · ${esc(d.question || "")}</div>`, "tl-sys");
      break;
  }
}
const traceBox = $("#trace-box");
traceBox.addEventListener("scroll", () => {
  const gap = traceBox.scrollHeight - traceBox.scrollTop - traceBox.clientHeight;
  traceStick = gap < 24;
  $("#trace-jump").hidden = traceStick || traceBox.scrollHeight <= traceBox.clientHeight + 2;
}, {passive: true});
$("#trace-jump").onclick = () => traceScroll(true);
function renderAuditEvent(e) {
  tracePush(e);
  if (e.type !== "agent.delta") {
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
  // 分页与 SSE 共用字节游标，重连直接 seek，不从文件头重扫。
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
      if (/task\.|decision\.|vuln\.|flag\.|platform\.|system\.|model\.call/.test(e.type || "")) scheduleStateRefresh();
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
  box.innerHTML = `<div class="empty-note">加载中…</div>`;
  try {
    const d = await api(`/api/sessions/${S.cur}/report`);
    box.innerHTML = `<div class="card report">${mdRender(d.markdown)}</div>`;
  } catch (e) {
    box.innerHTML = `<div class="empty-note">${esc(e.message)}</div>`;
  }
}

