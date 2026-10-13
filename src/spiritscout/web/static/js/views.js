/* ================= 视图：会话详情/拓扑/报告 ================= */
/* ================= 会话详情 ================= */
const TABS = ["chat", "topo", "vulns", "flags", "events", "report"];
function openSession(id) {
  S.cur = id; S.tab = "chat"; S.evSeen = 0; S.topoView = null;
  show("session");
  renderSessions();
  document.querySelectorAll("#s-tabs button").forEach(b =>
    b.classList.toggle("on", b.dataset.tab === "chat"));
  for (const t of TABS) $("#tab-" + t).hidden = t !== "chat";
  refreshState();
  startStreams();
}
document.querySelectorAll("#s-tabs button").forEach(b => b.onclick = () => {
  S.tab = b.dataset.tab;
  document.querySelectorAll("#s-tabs button").forEach(x => x.classList.toggle("on", x === b));
  for (const t of TABS) $("#tab-" + t).hidden = t !== S.tab;
  if (S.tab === "topo") drawTopo();
  if (S.tab === "report") loadReport();
});

async function refreshState() {
  if (!S.cur) return;
  try {
    S.state = await api(`/api/sessions/${S.cur}/state`);
    renderSessionHead(); renderChips(); renderApprovals(); renderQuestions();
    renderDash();
    if (S.tab === "topo") drawTopo();
    renderVulns(); renderFlags();
  } catch (e) { toast(e.message, true); }
}

/* 高风险动作即时确认：撞见高风险入口的分支挂起待裁决（G5 交互形态） */
const APV_CN = { reverse_shell: "反弹连接", webshell_upload: "上传 WebShell",
                 container_escape: "容器逃逸", brute_force: "口令爆破" };
function renderApprovals() {
  const list = (S.state && S.state.pending_approvals) || [];
  $("#topo-ops-apv").innerHTML = list.map(a => `
    <div class="apv">
      <div class="apv-title">⚠ 高风险动作待确认：${esc(APV_CN[a.action] || a.action)}</div>
      <div class="apv-detail">${esc(a.detail || "")}<br>允许后本次会话内该动作不再询问；拒绝只拦截本次调用，其余分支照常推进。</div>
      <div class="apv-ops">
        <button class="btn primary sm" data-apv="${esc(a.approval_id)}" data-allow="1">允许（本次会话）</button>
        <button class="btn sm" data-apv="${esc(a.approval_id)}" data-allow="0">拒绝</button>
      </div>
    </div>`).join("");
  $("#topo-ops-apv").querySelectorAll("[data-apv]").forEach(b => b.onclick = async () => {
    try {
      await api(`/api/sessions/${S.cur}/approvals/${b.dataset.apv}`,
        { method: "POST", body: JSON.stringify({ allow: b.dataset.allow === "1" }) });
      toast(b.dataset.allow === "1" ? "已放行，本次会话内该动作不再询问" : "已拒绝，该分支将绕过此动作");
      refreshState();
    } catch (e) { toast(e.message, true); refreshState(); }
  });
}

/* 模型发起的提问：问题与选项由模型撰写；勾选目标 = 用户授权扩围 */
function renderQuestions() {
  const box = $("#topo-ops-ask");
  if (!box) return;
  const list = (S.state && S.state.pending_questions) || [];
  box.innerHTML = list.map(q => `
    <div class="apv ask">
      <div class="apv-title">${esc(agentName(q.agent) || q.agent || "智能体")} 向你提问</div>
      <div class="apv-detail">${esc(q.question || "")}</div>
      ${q.targets && q.targets.length ? `<div class="ask-targets">${q.targets.map(t =>
        `<label class="ask-target"><input type="checkbox" data-qtarget="${esc(q.question_id)}" value="${esc(t)}">将 ${esc(t)} 加入本会话范围</label>`).join("")}</div>` : ""}
      <div class="ask-ops">
        ${(q.options || []).map((o, i) =>
          `<button class="btn primary sm" data-qask="${esc(q.question_id)}" data-qchoice="${i}">${esc(o)}</button>`).join("")}
      </div>
      <div class="ask-free"><input placeholder="或者直接输入你的意见…" data-qfree="${esc(q.question_id)}">
        <button class="btn sm" data-qsend="${esc(q.question_id)}">答复</button></div>
    </div>`).join("");
  box.querySelectorAll("[data-qask]").forEach(b => b.onclick = () =>
    answerQuestion(b.dataset.qask, { choice: Number(b.dataset.qchoice),
                                     addTargets: collectTargets(b.dataset.qask) }));
  box.querySelectorAll("[data-qsend]").forEach(b => b.onclick = () => {
    const input = box.querySelector(`[data-qfree="${b.dataset.qsend}"]`);
    const text = (input.value || "").trim();
    if (!text) { toast("先填写答复内容", true); return; }
    answerQuestion(b.dataset.qsend, { text, addTargets: collectTargets(b.dataset.qsend) });
  });
}
function collectTargets(qid) {
  return [...document.querySelectorAll(`[data-qtarget="${qid}"]:checked`)].map(c => c.value);
}
async function answerQuestion(qid, body) {
  try {
    await api(`/api/sessions/${S.cur}/questions/${qid}`,
      { method: "POST", body: JSON.stringify(body) });
    toast("已答复，智能体将继续");
    refreshState();
  } catch (e) { toast(e.message, true); refreshState(); }
}

/* 仪表盘：实时 token 消耗与会话态势（数据面 = /state） */
const fmtK = n => n >= 1e6 ? (n / 1e6).toFixed(2) + "M"
  : n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n ?? 0);
function renderDash() {
  const box = $("#dash-body");
  if (!box) return;
  const list = S.sessions || [];
  const running = list.filter(x => !TERMINAL.has(x.status)).length;
  const tokens = list.reduce((a, x) => a + (x.tokens || 0), 0);
  const cost = list.reduce((a, x) => a + (x.cost_usd || 0), 0);
  const flagCell = f => !f || !f.total ? "—"
    : (f.submitted ? `${f.accepted}/${f.submitted}` : `${f.total}`);
  box.innerHTML = `
    <div class="tiles">
      <div class="tile"><span class="tk">token</span><b class="num">${fmtK(tokens)}</b><span class="mu">全部会话累计</span></div>
      <div class="tile"><span class="tk">费用</span><b class="num">$${cost.toFixed(3)}</b><span class="mu">按单价估算</span></div>
      <div class="tile"><span class="tk">会话</span><b class="num">${list.length}</b><span class="mu">${running} 个运行中</span></div>
    </div>
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
        <td class="mono">${fmtDur(x.started_at, x.finished_at)}</td></tr>`).join("")}</tbody></table>`
      : '<div class="empty-note">还没有会话——回「任务」页下达第一个任务</div>'}`;
  box.querySelectorAll("[data-open]").forEach(row =>
    row.onclick = () => openSession(row.dataset.open));
}

function renderSessionHead() {
  const s = S.state.session;
  $("#s-title").textContent = s.title || s.target || s.session_id;
  $("#s-title").title = s.target || "";
  $("#op-rename").onclick = () => startRename(S.cur, $("#s-title"));
  $("#s-meta").innerHTML = [
    `<span class="pill ${s.status}">${STATUS_CN[s.status] || s.status}</span>`,
    `<span class="tag blue">${MODE_CN[s.mode] || s.mode}</span>`,
    s.started_at ? `<span class="tag">用时 ${fmtDur(s.started_at, s.finished_at)}</span>` : "",
    `<span class="tag mono">${esc(s.session_id)}</span>`,
  ].join("");
  $("#s-err").innerHTML = s.error ? `<div class="err-banner">${esc(s.error)}</div>` : "";
  const running = !TERMINAL.has(s.status);
  $("#comp-send").disabled = false;
  $("#comp-attach").disabled = false;
  $("#comp-input").placeholder = running
    ? "给灵枢提意见、补充线索或上传附件…"
    : "会话已结束——继续提问或布置新方向，灵枢带着现场重新开工";
  $("#s-ops").innerHTML = `
    ${running ? `<button class="btn danger sm" id="op-stop">停止</button>` : ""}
    <a class="btn sm" href="/api/sessions/${S.cur}/report/download" download>下载报告</a>
    <a class="btn sm" href="/api/sessions/${S.cur}/audit/download" download>导出审计</a>
    <button class="btn danger sm" id="op-del" ${running ? "disabled" : ""}>删除</button>`;
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
  const phase = { queued: "等待调度", starting: "启动任务", mode_routing: "判断任务类型",
    model_preflight: "检查模型", runtime_starting: "初始化工具与智能体",
    executing: "执行中", completed: "已完成", failed: "失败", stopped: "已停止",
    interrupted: "已中断" }[st.session.phase] || st.session.phase || "等待启动";
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
  const nodes = tp.dag_nodes || [], tasks = tp.tasks || [];
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
        <span class="what">${esc(TASK_CN[t.task_type] || t.task_type || "")}</span>
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

  // 发现组：共享笔记/入口/Flag 落黑板后作为互连元素挂在最右列（链式相连）
  const findings = (S.state && S.state.findings) || [];
  // 概括显示：入口/资产去重合并，笔记与 Flag 只留最近的——不把 URL 刷成一片
  const uniqAssets = [...new Map(
    findings.filter(f => f.type === "asset")
            .map(f => [String(f.url || f.text || ""), f])).values()];
  const dispFindings = [];
  if (uniqAssets.length) {
    dispFindings.push({ type: "asset", count: uniqAssets.length,
      text: uniqAssets.slice(0, 2).map(f => String(f.url || f.text).slice(0, 70)).join("\n")
            + (uniqAssets.length > 2 ? `\n…等 ${uniqAssets.length} 个入口` : "") });
  }
  findings.filter(f => f.type === "note").slice(-4)
          .forEach(f => dispFindings.push(f));
  findings.filter(f => f.type === "flag").slice(-1)
          .forEach(f => dispFindings.push(f));
  const FNODE = i => `__finding_${i}`;
  if (dispFindings.length) {
    const layerIdx = layerIds.length;
    layerIds.push([]);
    dispFindings.forEach((f, i) => {
      const id = FNODE(i);
      layerIds[layerIdx].push(id);
      depthIndex.set(id, layerIdx);
      const kind = ({ note: "笔记", flag: "Flag", asset: `入口 ×${f.count || 1}` })[f.type]
        || f.type || "发现";
      nodes.push({ node_id: id, title: String(f.text || "").slice(0, 90),
                   tactic: kind, status: f.type === "flag" ? "done" : "running",
                   depends_on: [] });
    });
  }
  const ids = [...depthIndex.keys()];
  if (!ids.length) {
    box.innerHTML = `<div class="empty-note">等待灵枢规划——任务拓扑将在此实时展开</div>${OPS}`;
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
  // 发现连线：从锚点（路线层/任务层首个节点）星形展开到每个发现节点
  if (dispFindings.length) {
    const prevLayer = layerIds[depthIndex.get(FNODE(0)) - 1] || [];
    const anchor = prevLayer.find(id => id !== ORPHAN) || prevLayer[0];
    if (anchor) {
      for (let i = 0; i < dispFindings.length; i++) {
        preds.get(FNODE(i)).push(anchor);
        succs.get(anchor).push(FNODE(i));
      }
    }
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
        <div class="tact"><span class="dot ${st}"></span><b>${esc(agentName(r.agent))} · ${esc(r.name || "")}</b>
          <span class="tac">${pct}%</span></div>
        <div class="prog ${pct === 100 ? "done" : ""}"><i style="width:${pct}%"></i></div>
        ${r.note ? `<div class="rt-note">${esc(isExp ? r.note : (r.note || "").slice(0, 120))}</div>` : ""}
        ${digest ? `<div class="mind-digest">✦ ${esc(digest)}</div>` : ""}
      </div>`;
    }
    const st = nodeStatus(n.status);
    return `<div class="tnode ${st === "active" ? "active" : ""}" data-node="${esc(n.node_id)}"
      style="visibility:hidden">
      <div class="tact"><span class="dot ${st}"></span><b>${esc(n.title || n.tactic)}</b>
        <span class="tac">${esc(n.tactic || "")}</span></div>
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

  box.innerHTML = `<div id="topo-stage">
      <svg class="edges"></svg>
      ${unodeHtml}
      <div class="hub ${running ? "live" : ""}" id="topo-hub" style="visibility:hidden"><b>灵枢</b><small>主 Agent · 集中决策</small></div>
      ${ids.filter(id => id !== ORPHAN).map(id => cardHtml(nodeOf.get(id))).join("")}
      ${orphans.length ? orphanCard : ""}
    </div>
    <div class="topo-ops" id="topo-ops"><div id="topo-ops-apv"></div><div id="topo-ops-ask"></div></div>
    <div class="topo-tools">
      <button id="topo-zin" title="放大">＋</button>
      <button id="topo-zout" title="缩小">－</button>
      <button id="topo-fit" title="适应窗口">⤢</button>
    </div>
    <div class="topo-hint">滚轮缩放 · 拖拽平移 · 双击复位</div>
    <div class="rnode" id="topo-ring" style="visibility:hidden"
      title="环形共享台账：已尝试登记 ${ring.attempts} 条，同伴无产出的入口自动避让">
      <b>环形台账</b><span class="rn">${ring.attempts} 条登记</span></div>`;
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
  const ringEl = box.querySelector("#topo-ring");
  if (ringEl) {
    ringEl.style.transform = `translate(${hubXpos}px, ${centerY + hubH / 2 + 16}px)`;
    ringEl.style.visibility = "";
  }
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
  box.addEventListener("pointerdown", (ev) => {
    ev.preventDefault();
    if (ev.button !== 0 || !S.topoView || ev.target.closest(".topo-tools")) return;
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
  const stop = () => { drag = null; box.classList.remove("grabbing"); };
  box.addEventListener("click", (ev) => {
    const node = ev.target.closest("[data-node]");
    if (!node || ev.target.closest(".topo-tools")) return;
    const id = node.dataset.node;
    S.topoExpand = S.topoExpand || {};
    S.topoExpand[id] = !S.topoExpand[id];
    drawTopo();
  });
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

function renderVulns() {
  const vs = S.state.vulns.items || [];
  $("#tab-vulns").innerHTML = vs.length ? `<div class="grid-cards">${vs.map(v => {
    const [sevCn, sevCls] = SEV_CN[v.severity] || [v.severity || "未定级", "blue"];
    return `<div class="card vcard">
      <div class="h"><b>${esc(v.title || v.vuln_type || "漏洞")}</b>
        <span class="tag ${sevCls}">${sevCn}</span>
        <span class="tag ${v.status === "accepted" ? "green" : ""}">${esc(v.status || "")}</span>
        ${v.poc_verified ? `<span class="tag gold">PoC 已验证</span>` : ""}</div>
      ${v.endpoint ? `<div class="ep">${esc(v.endpoint)}</div>` : ""}
      <div class="muted" style="font-size:11.5px">${esc(v.vuln_type || "")}${v.cvss_score != null ? " · CVSS " + v.cvss_score : ""} · ${esc(v.vuln_id || "")}</div>
    </div>`; }).join("")}</div>`
    : `<div class="empty-note">尚无漏洞记录</div>`;
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
function traceReset() {
  $("#trace-box").innerHTML = '<div class="empty-note">等待灵枢开工——过程将按真实时间顺序在此展开</div>';
  TRACE_AGENT_SLOTS.clear();
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
  return box.lastElementChild;
}
function traceScroll(force = false) {
  const box = $("#trace-box");
  if (!box) return;
  if (force || traceStick) {
    box.scrollTop = box.scrollHeight;
    traceStick = true;
  }
  $("#trace-jump").hidden = traceStick || box.scrollHeight <= box.clientHeight + 2;
}
function traceTurn(actor) {
  const rows = $("#trace-box").querySelectorAll(".tl-turn");
  for (let i = rows.length - 1; i >= 0; i--)
    if (rows[i].dataset.agent === String(actor || "")) return rows[i];
  return null;
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
  const compact = one(JSON.stringify(args));
  return compact === "{}" ? "执行" : compact.slice(0, 180);
}
function tracePush(e) {
  const box = $("#trace-box");
  if (!box) return;
  const d = (typeof e.detail === "object" && e.detail) || {};
  const actor = String(e.actor || "");
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
      const turn = traceTurn(actor);
      if (!turn) break;
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
        // 推理过程聚合到拓扑页的推理栏（renderMind），会话流只留一枚计数章
        let rec = S.think.get(actor);
        if (!rec) { rec = { chars: 0, tail: "" }; S.think.set(actor, rec); }
        rec.chars += text.length;
        rec.tail = (rec.tail + text).slice(-6000);
        let chip = flow.querySelector(".tl-thinkchip");
        if (!chip) {
          flow.insertAdjacentHTML("beforeend",
            '<div class="tl-thinkchip" title="推理过程在「拓扑」页右侧推理栏查看"></div>');
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
      const turn = traceTurn(actor);
      if (!turn) break;
      traceFinishThinking(turn);
      const flow = turn.querySelector(".flow");
      for (const c of (d.calls || [])) {
        flow.insertAdjacentHTML("beforeend", `<div class="tl-act" data-id="${esc(c.id || "")}">
          <button class="hd" type="button" aria-expanded="false"><span class="nm">${esc(c.name || "?")}</span>
            <span class="ar">${esc(argBrief(c.name, c.arguments))}</span><span class="st">进行中</span>
            <span class="cv" aria-hidden="true">⌄</span></button><div class="nt"></div><pre></pre></div>`);
        const act = flow.lastElementChild;
        const btn = act.querySelector("button.hd");
        btn.onclick = () => {
          act.classList.toggle("open");
          btn.setAttribute("aria-expanded", String(act.classList.contains("open")));
        };
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
      if (!el) break;
      const st = el.querySelector(".st");
      st.textContent = d.ok ? "完成" : "失败";
      st.className = "st " + (d.ok ? "y" : "n");
      el.querySelector(".nt").textContent = String(d.note || "");
      el.querySelector("pre").textContent = String(d.result ?? "");
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
    case "task.done":
      traceFinishThinking(traceTurn(actor));
      traceAppend(e, `<div class="m">${esc(actorName(actor))} 已完成任务</div>`, "tl-sys");
      break;
    case "user.message":
      traceAppend(e, `<div class="bubble"><div class="btxt">${esc(d.content || "")}</div></div>`, "tl-user");
      break;
    case "system.lifecycle":
      if (d.action === "session_kickoff")
        traceAppend(e, `<div class="m">灵枢开工准备中——检查模型链路并判定任务模式…</div>`, "tl-sys");
      break;
    case "user.attachment": {
      const sz = fmtSize(d.size);
      traceAppend(e, `<div class="bubble"><div class="btxt">📎 ${esc(d.name || "")}${sz ? ` <span class="k">${esc(sz)}</span>` : ""}</div>
        ${d.path ? `<div class="bpath">${esc(d.path)}</div>` : ""}</div>`, "tl-user");
      break;
    }
    case "ring.steer":
      traceAppend(e, `<div class="hd"><span class="who">灵枢</span>
        <span class="k">→ ${esc(agentName(d.agent))}</span></div>
        <div class="tl-reply">${esc(d.hint || "")}</div>`, "tl-master");
      break;
    case "decision.change": {
      if (d.action === "llm_mode_route") {
        traceAppend(e, `<div class="hd"><span class="who">灵枢</span>
          <span class="k">任务模式判定</span></div>
          <div class="ang">→ ${esc(MODE_CN[d.llm_mode] || d.llm_mode || "?")}${d.rationale ? ` · ${esc(d.rationale)}` : ""}</div>`, "tl-master");
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
      traceAppend(e, `<div class="tl-reply md">${mdRender(d.text || "")}</div>`, "tl-master");
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
function startStreams() {
  stopStreams();
  if (!S.cur) return;
  traceReset();
  const es = new EventSource(`/api/sessions/${S.cur}/events`);
  S.es = es;
  // 审计帧是具名事件（event: audit），onmessage 收不到——必须按事件名监听
  es.addEventListener("audit", m => {
    try {
      const e = JSON.parse(m.data);
      if (e.seq <= S.evSeen) return;
      S.evSeen = e.seq;
      tracePush(e);
      if (e.type !== "agent.delta") {       // 流式文本进轨迹，原始事件面板不刷屏
        const box = $("#events");
        box.insertAdjacentHTML("afterbegin", evLine(e));
        while (box.children.length > 250) box.lastChild.remove();
      }
      if (/task\.|decision\.|vuln\.|flag\.|system\./.test(e.type || "")) refreshState();
    } catch {}
  });
  es.addEventListener("done", () => { refreshState(); });
  es.onerror = () => {};
  S.poll = setInterval(() => { refreshState(); refreshSessions(); }, 3000);
}
function stopStreams() {
  if (S.es) { S.es.close(); S.es = null; }
  if (S.poll) { clearInterval(S.poll); S.poll = null; }
}

/* ================= 报告 ================= */
function mdRender(md) {
  const escT = s => esc(s);
  const lines = md.split("\n");
  let html = "", inCode = false, inTable = false, list = 0;
  const inline = s => escT(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
  const closeList = () => { while (list) { html += "</ul>"; list--; } };
  for (const ln of lines) {
    if (/^```/.test(ln)) {
      if (inCode) html += "</pre>"; else { closeList(); html += "<pre>"; }
      inCode = !inCode; continue;
    }
    if (inCode) { html += escT(ln) + "\n"; continue; }
    const h = ln.match(/^(#{1,4})\s+(.*)/);
    if (h) { closeList(); const lv = h[1].length; html += `<h${lv}>${inline(h[2])}</h${lv}>`; continue; }
    if (/^\s*\|/.test(ln)) {          // 表格行：容忍缺尾 | 的行（模型产出常见）
      closeList();
      const cells = ln.trim().replace(/^\|/, "").replace(/\|\s*$/, "")
        .split("|").map(c => c.trim());
      if (cells.every(c => /^:?-{2,}:?$/.test(c))) continue;   // 分隔行不渲染
      if (!inTable) { html += "<table>"; inTable = true; }
      html += "<tr>" + cells.map(c => `<td>${inline(c)}</td>`).join("") + "</tr>";
      continue;
    }
    if (inTable) { html += "</table>"; inTable = false; }
    const li = ln.match(/^(\s*)[-*]\s+(.*)/);
    if (li) {
      const d = Math.floor(li[1].length / 2);
      while (list < d + 1) { html += "<ul>"; list++; }
      while (list > d + 1) { html += "</ul>"; list--; }
      html += `<li>${inline(li[2])}</li>`; continue;
    }
    closeList();
    if (/^---+\s*$/.test(ln)) { html += "<hr>"; continue; }
    if (ln.trim()) html += `<p>${inline(ln)}</p>`;
  }
  closeList();
  if (inCode) html += "</pre>";
  if (inTable) html += "</table>";
  return html;
}
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

