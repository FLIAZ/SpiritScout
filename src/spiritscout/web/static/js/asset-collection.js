"use strict";
const AssetCollectionPage = (() => {
  let llm = "", models = [];
  let proxyPlans = [], proxyPlan = "direct";
  const modelPreference = "spiritscout.asset-collection.model";
  const llmLabel = ref => { const item = models.find(m => (m.key || m.model) === ref); return item ? I18n.t`${item.provider_ref || item.provider || I18n.t("默认供应商")} / ${item.model}` : ref ? I18n.t("所选模型已不可用") : I18n.t("未选择"); };
  function saveModel() { try { localStorage.setItem(modelPreference, llm); } catch (_) { /* 浏览器禁用存储时保留当前页面选择。 */ } }
  let timer = null, project = "", campaigns = [], selected = "", source = null;
  let active = false, generation = 0, request = 0, loading = false, cursor = 0;
  let streamRun = undefined, resultsRequest = 0, resultsCount = -1;
  let activeCommand = null, commandBlocks = [], assetNotice = null;
  let resultData = [], resultPage = 1, pageSize = 15, resultFilter = "all";
  const busy = new Set();
  const root = () => $("#view-asset-collection");
  const statusText = {queued: I18n.t("待启动"), running: I18n.t("运行中"), paused: I18n.t("已暂停下一步"), completed: I18n.t("已完成"), completed_with_errors: I18n.t("完成但有错误"), failed: I18n.t("失败"), stopped: I18n.t("已停止")};
  const httpResponded = item => ["http_responded", "http_verified"].includes(item.verification_status);
  const terminal = c => !["running", "paused", "queued"].includes(c.status);
  const valid = g => active && generation === g;
  const timeText = value => value ? new Date(value * 1000).toLocaleString(I18n.dateLocale) : "—";

  function closeStream() { if (source) source.close(); source = null; }
  function clearDetail() {
    activeCommand = null; commandBlocks = []; assetNotice = null; resultData = []; resultPage = 1;
    closeStream(); selected = ""; cursor = 0; streamRun = undefined; resultsCount = -1; resultsRequest++;
    root().querySelector("[data-detail]").hidden = true;
  }
  function message(text = "") {
    const el = root().querySelector("[data-error]");
    el.textContent = text; el.hidden = !text;
  }
  function shell() {
    root().innerHTML = I18n.html`<header class="resource-head"><div><h2>资产收集</h2><p>AI 读取目标并选择收集命令与引擎，实时查看后台输出。</p></div><div class="resource-head-actions"><button class="btn" data-refresh>刷新</button><button class="btn primary" data-new disabled>新建收集任务</button></div></header><div class="asset-collection-toolbar"><label>项目<select data-project aria-label="收集项目"><option value="">正在加载项目…</option></select></label><label>收集 LLM<select data-model aria-label="资产收集 LLM 模型"><option value="">正在加载模型…</option></select></label><label>主动收集出口<select data-proxy aria-label="主动收集代理出口"><option value="direct">直连（不使用代理）</option></select></label><span data-summary></span></div><p class="muted">模型仅用于资产收集；切换选择影响新建或重新运行的任务，不改变运行中的任务。密钥沿用「模型」页的供应商配置。</p><p class="muted">代理可选；新建批次的主动请求使用所选出口，既有批次保持创建时出口。第三方情报结果不代表本次出口实测；出口不可用时不会静默改走直连。</p><p class="asset-collection-error" data-error role="alert" hidden></p><div class="asset-collection-workspace"><div class="asset-collection-grid" data-list></div><section class="asset-collection-detail" data-detail hidden><header class="asset-output-head"><div><h3 data-title></h3><p data-meta></p></div><span data-connection></span></header><p class="asset-collection-error" data-failure hidden></p><div data-approvals></div><section class="asset-usage" aria-label="资产收集模型用量"><dl><div><dt>累计 Token</dt><dd data-usage-total>—</dd></div><div><dt>输入 Token</dt><dd data-usage-input>—</dd></div><div><dt>输出 Token</dt><dd data-usage-output>—</dd></div><div><dt>缓存命中输入</dt><dd data-usage-cached>未提供</dd></div><div><dt>输入缓存命中率</dt><dd data-usage-rate>未提供</dd></div></dl><p data-usage-coverage></p></section><div class="asset-output-toolbar"><b>收集进展</b><label><input type="checkbox" data-follow checked> 跟随最新输出</label></div><div class="asset-output-log" data-output tabindex="0" role="log" aria-label="后台收集输出"></div><h3 class="asset-results-title">收集结果 <small data-result-count></small></h3><div class="asset-results-actions"><a class="btn sm" data-export>导出 CSV（含状态码与出口）</a><button class="btn primary sm" data-src disabled>选择资产开始 SRC 会话</button></div><p class="muted" data-excluded></p><div class="asset-results-controls"><label>显示<select data-result-filter aria-label="资产存活筛选"><option value="all">全部资产</option><option value="http_responded">HTTP 已响应</option><option value="dns_resolved">DNS 已解析</option><option value="unverified">待验证</option><option value="verification_failed">验证失败</option></select></label><label>每页<select data-page-size aria-label="每页资产数量">${[10,15,20,50,100].map(n => `<option value="${n}" ${n===pageSize?"selected":""}>${n}</option>`).join("")}</select></label><span data-page-info></span><button class="btn sm" data-page-prev>上一页</button><button class="btn sm" data-page-next>下一页</button></div><div class="asset-collection-results" data-results></div></section></div>`;
    root().querySelector("[data-project]").onchange = e => {
      project = e.target.value; clearDetail(); campaigns = []; render();
      load().catch(() => {});
    };
    root().querySelector("[data-model]").onchange = e => {
      llm = e.target.value; saveModel();
      root().querySelector("[data-new]").disabled = !project || !llm;
      render();
    };
    root().querySelector("[data-refresh]").onclick = () => load().then(() => { if (active && selected) loadResults(selected); }).catch(() => {});
    root().querySelector("[data-new]").onclick = createDialog;
    root().querySelector("[data-proxy]").onchange = e => { proxyPlan = e.target.value; };
    root().querySelector("[data-src]").onclick = () => document.dispatchEvent(new CustomEvent("asset-collection:src", {detail: {project_id: project, campaign_id: selected}}));
    root().querySelector("[data-page-size]").onchange = e => { pageSize = Number(e.target.value); resultPage = 1; renderResults(); };
    root().querySelector("[data-result-filter]").onchange = e => { resultFilter = e.target.value; resultPage = 1; renderResults(); };
    root().querySelector("[data-page-prev]").onclick = () => { resultPage--; renderResults(); };
    root().querySelector("[data-page-next]").onclick = () => { resultPage++; renderResults(); };
    const output = root().querySelector("[data-output]");
    output.addEventListener("scroll", () => {
      root().querySelector("[data-follow]").checked = output.scrollHeight - output.scrollTop - output.clientHeight < 24;
    });
    root().querySelector("[data-follow]").onchange = e => { if (e.target.checked) output.scrollTop = output.scrollHeight; };
  }

  async function load() {
    const g = generation, req = ++request;
    loading = true;
    try {
      const [data, modelData, proxyData] = await Promise.all([api("/api/projects"), api("/api/models"), api("/api/proxies")]);
      if (!valid(g) || req !== request) return;
      proxyPlans = proxyData.plans || [];
      const proxySelect = root().querySelector("[data-proxy]");
      const proxyOptions = I18n.html('<option value="direct">直连（不使用代理）</option>') + proxyPlans.map(p => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("");
      if (proxySelect.dataset.options !== proxyOptions) { proxySelect.innerHTML = proxyOptions; proxySelect.dataset.options = proxyOptions; }
      if (proxyPlan !== "direct" && !proxyPlans.some(p => p.id === proxyPlan)) proxyPlan = "direct";
      proxySelect.value = proxyPlan;
      if (typeof selxSync === "function") selxSync(proxySelect);
      models = (modelData.models || []).filter(m => m.key || m.model);
      const modelSelect = root().querySelector("[data-model]");
      const modelOptions = I18n.html`<option value="">${models.length ? I18n.html("请选择收集模型") : I18n.html("暂无模型，请先在模型页配置")}</option>` + models.map(m => `<option value="${esc(m.key || m.model)}">${esc(llmLabel(m.key || m.model))}</option>`).join("");
      if (modelSelect.dataset.options !== modelOptions) { modelSelect.innerHTML = modelOptions; modelSelect.dataset.options = modelOptions; }
      if (llm && !models.some(m => (m.key || m.model) === llm)) { llm = ""; saveModel(); }
      modelSelect.value = llm; modelSelect.disabled = !models.length;
      if (typeof selxSync === "function") selxSync(modelSelect);
      const projects = (data.projects || []).filter(p => p.project_id && !p.read_only)
        .sort((a, b) => Number(b.created_at || 0) - Number(a.created_at || 0));
      if (!projects.some(p => p.project_id === project)) {
        project = projects[0]?.project_id || ""; clearDetail(); campaigns = []; render();
      }
      const select = root().querySelector("[data-project]");
      const options = projects.length ? projects.map(p => `<option value="${esc(p.project_id)}">${esc(p.name)}</option>`).join("") : I18n.html`<option value="">暂无项目，请先在项目页创建</option>`;
      if (select.dataset.options !== options) { select.innerHTML = options; select.dataset.options = options; }
      select.value = project; select.disabled = !projects.length;
      // 原生 select 已被全局控件包装，异步填充选项后必须同步可见标签。
      if (typeof selxSync === "function") selxSync(select);
      root().querySelector("[data-new]").disabled = !project || !llm;
      const requestedProject = project;
      const result = project ? await api(`/api/projects/${encodeURIComponent(project)}/asset-campaigns`) : {campaigns: []};
      if (!valid(g) || req !== request || project !== requestedProject) return;
      campaigns = (result.campaigns || []).map(c => {
        const newer = campaigns.find(item => item.campaign_id === c.campaign_id && item.updated_at > c.updated_at);
        return newer || c;
      }); loading = false; message(); render();
      if (!campaigns.some(c => c.campaign_id === selected)) {
        clearDetail();
        const first = campaigns.find(c => ["running", "paused"].includes(c.status)) || campaigns[0];
        if (first) selectCampaign(first.campaign_id);
      } else updateDetail(campaigns.find(c => c.campaign_id === selected));
    } catch (e) {
      if (valid(g) && req === request) message(I18n.t`刷新失败，当前内容可能不是最新状态：${e.message}`);
      throw e;
    } finally { if (req === request) loading = false; }
  }

  function cardHTML(c) {
    const s = c.stats || {};
    const primary = c.status === "running" ? I18n.html`<button class="btn sm" data-action="pause">暂停下一步</button>` : I18n.html`<button class="btn sm" data-action="resume">${c.status === "queued" ? I18n.html("启动") : ["paused"].includes(c.status) ? I18n.html("继续") : I18n.html("重新运行")}</button>`;
    const stop = ["running", "paused", "queued"].includes(c.status) ? I18n.html`<button class="btn sm danger" data-action="stop">停止</button>` : "";
    return I18n.html`<div class="asset-campaign-title"><b>${esc((c.targets || []).join("、") || I18n.html("资产收集任务"))}</b><span data-status="${esc(c.status)}">${statusText[c.status] || esc(c.status)}</span></div><p class="asset-campaign-id">${esc(c.campaign_id)} · ${esc(timeText(c.created_at))}</p><p>LLM：${esc(llmLabel(c.config?.model))}</p><p>${Number(s.commands || 0)} 次命令/工具调用 · ${Number(s.candidates || 0)} 个资产</p>${c.error ? `<p class="asset-campaign-error">${esc(c.error)}</p>` : ""}<div class="asset-campaign-actions">${primary}${stop}<button class="btn sm" data-output-open>输出与结果</button><button class="btn sm danger" data-delete>删除任务</button></div>`;
  }
  function render() {
    const list = root().querySelector("[data-list]");
    root().querySelector("[data-summary]").textContent = loading ? I18n.t("加载中…") : I18n.t`${campaigns.length} 个收集批次`;
    list.querySelector(".resource-empty")?.remove();
    for (const node of [...list.querySelectorAll("[data-campaign]")]) {
      if (!campaigns.some(c => c.campaign_id === node.dataset.campaign)) node.remove();
    }
    for (const c of campaigns) {
      let node = [...list.children].find(n => n.dataset.campaign === c.campaign_id);
      if (!node) { node = document.createElement("article"); node.className = "asset-campaign-card"; node.dataset.campaign = c.campaign_id; list.appendChild(node); }
      const html = cardHTML(c);
      if (node._content !== html) {
        node.innerHTML = html; node._content = html;
        node.querySelectorAll("[data-action]").forEach(button => button.onclick = () => action(c.campaign_id, button.dataset.action));
        node.querySelector("[data-output-open]").onclick = () => selectCampaign(c.campaign_id);
        node.querySelector("[data-delete]").onclick = () => remove(c.campaign_id);
      }
      node.classList.toggle("selected", c.campaign_id === selected);
      node.querySelectorAll("button").forEach(b => { b.disabled = busy.has(c.campaign_id) || (b.dataset.action === "resume" && c.status !== "paused" && !llm); });
    }
    if (!campaigns.length) list.innerHTML = I18n.html`<div class="resource-empty">${project ? I18n.html("当前项目还没有收集任务，点击「新建收集任务」开始。") : I18n.html("请先在项目页创建项目。")}</div>`;
  }
  function selectCampaign(id) {
    if (id === selected) return;
    clearDetail(); selected = id;
    const detail = root().querySelector("[data-detail]"); detail.hidden = false;
    detail.querySelector("[data-output]").replaceChildren();
    renderResults();
    detail.querySelector("[data-results]").textContent = I18n.t("正在加载结果…");
    detail.querySelector("[data-follow]").checked = true;
    render(); updateDetail(campaigns.find(c => c.campaign_id === id));
  }
  function updateUsage(c) {
    root().querySelector("[data-src]").disabled = !terminal(c) || !Number(c.stats?.candidates);
    root().querySelector("[data-export]").href = `/api/asset-campaigns/${encodeURIComponent(c.campaign_id)}/export.csv`;
    const usage = c.stats?.usage, cache = usage?.cache;
    const count = value => typeof value === "number" && Number.isFinite(value) ? value.toLocaleString(I18n.dateLocale) : "—";
    const detail = root().querySelector("[data-detail]");
    detail.querySelector("[data-usage-total]").textContent = count(usage?.used_tokens);
    detail.querySelector("[data-usage-input]").textContent = count(cache?.total_input_tokens);
    detail.querySelector("[data-usage-output]").textContent = count(cache?.output_tokens);
    const measured = Number(cache?.measured_calls || 0), unknown = Number(cache?.unmeasured_calls || 0);
    detail.querySelector("[data-usage-cached]").textContent = measured ? count(cache.cached_input_tokens) : I18n.t("未提供");
    detail.querySelector("[data-usage-rate]").textContent = typeof cache?.hit_rate === "number" && Number.isFinite(cache.hit_rate)
      ? `${(cache.hit_rate * 100).toFixed(1)}%` : I18n.t("未提供");
    detail.querySelector("[data-usage-coverage]").textContent = !usage ? I18n.t("尚无模型用量数据。") :
      I18n.t`缓存计量：${measured} 次已提供，${unknown} 次未提供。命中率仅按端点已提供缓存计量的输入计算，不含输出 Token；不代表工具结果复用率。`;
  }
  function outputLine(row) {
    const line = document.createElement("div"); line.className = "asset-output-line"; line.dataset.level = row.level;
    const time = document.createElement("time"); time.textContent = timeText(row.created_at);
    const text = document.createElement("span"); text.textContent = row.message;
    line.append(time, text); return line;
  }
  function commandBlock(row, ref, purpose) {
    const block = document.createElement("details"); block.className = "asset-command-block";
    block.dataset.execution = ref; block.dataset.seq = row.seq;
    const summary = document.createElement("summary");
    const label = document.createElement("b"); label.textContent = I18n.t("[使用了命令]");
    const title = document.createElement("span"); title.textContent = purpose; title.title = purpose;
    const status = document.createElement("small"); status.textContent = I18n.t("执行中");
    summary.append(label, title, status);
    const body = document.createElement("div"); body.className = "asset-command-body";
    block.append(summary, body); root().querySelector("[data-output]").appendChild(block);
    const item = {block, body, status, ref, ended: false}; commandBlocks.push(item); return item;
  }
  function appendOutput(row) {
    const log = root().querySelector("[data-output]");
    if (!log.children.length) log.textContent = "";
    if (row.level === "asset") {
      if (!assetNotice) { assetNotice = outputLine(row); log.appendChild(assetNotice); }
      assetNotice.children[0].textContent = timeText(row.created_at);
      assetNotice.children[1].textContent = I18n.t`资产已实时入库；最新：${row.message}。详细结果见下方列表。`;
      if (root().querySelector("[data-follow]").checked) log.scrollTop = log.scrollHeight;
      return;
    }
    if (row.level === "agent" || row.level === "command") assetNotice = null;
    // 旧批次只有 level/message；执行标识只解析宿主日志格式，不理解或改写工具内容。
    const start = row.level === "command" && /^(execution-\d+) · (.*)$/s.exec(row.message);
    const end = /^(execution-\d+) · 退出码 (-?\d+) · (.*)$/s.exec(row.message);
    if (start) {
      if (activeCommand) { activeCommand.status.textContent = I18n.t("执行记录未完整返回"); activeCommand.ended = true; }
      activeCommand = commandBlock(row, start[1], start[2]);
      activeCommand.body.appendChild(outputLine(row));
    } else if (end && activeCommand?.ref === end[1]) {
      activeCommand.body.appendChild(outputLine(row)); activeCommand.ended = true;
      const failed = row.level === "error" || Number(end[2]) !== 0;
      activeCommand.block.dataset.status = failed ? "failed" : "completed";
      activeCommand.status.textContent = failed ? I18n.t`失败 · 退出码 ${end[2]}` : I18n.t("执行完成");
      if (failed) { activeCommand.status.title = end[3]; activeCommand.status.textContent += ` · ${end[3].slice(0, 100)}`; }
      activeCommand = null;
    } else if (["command", "stdout", "stderr"].includes(row.level) ||
               activeCommand && !["agent", "asset"].includes(row.level)) {
      if (!activeCommand) activeCommand = commandBlock(row, `output-${row.seq}`, I18n.t("原始命令输出"));
      const body = activeCommand.body, follow = body.scrollHeight - body.scrollTop - body.clientHeight < 24;
      body.appendChild(outputLine(row)); if (follow) body.scrollTop = body.scrollHeight;
    } else log.appendChild(outputLine(row));
    if (root().querySelector("[data-follow]").checked) log.scrollTop = log.scrollHeight;
  }
  function finishCommandBlocks(status) {
    if (!["completed", "completed_with_errors", "failed", "stopped"].includes(status)) return;
    for (const item of commandBlocks) if (!item.ended) {
      item.ended = true; item.block.dataset.status = status;
      item.status.textContent = status === "stopped" ? I18n.t("任务已停止") : I18n.t("任务已结束，执行记录未完整返回");
    }
    activeCommand = null;
  }
  function updateDetail(c) {
    if (!c || selected !== c.campaign_id) return;
    updateUsage(c); finishCommandBlocks(c.status);
    const d = root().querySelector("[data-detail]");
    d.querySelector("[data-title]").textContent = (c.targets || []).join("、") || I18n.t("资产收集任务");
    d.querySelector("[data-meta]").textContent = I18n.t`${statusText[c.status] || c.status} · LLM：${llmLabel(c.config?.model)} · 主动出口：${c.config?.egress?.name || (c.config?.proxy_plan_id === "direct" ? I18n.t("直连") : c.config?.proxy_plan_id || I18n.t("历史未记录"))}${c.config?.egress?.exit_ip ? `（${c.config.egress.exit_ip}）` : ""} · ${c.campaign_id} · 开始 ${timeText(c.started_at)}${c.finished_at ? I18n.t` · 结束 ${timeText(c.finished_at)}` : ""}`;
    const failure = d.querySelector("[data-failure]"); failure.textContent = c.error || ""; failure.hidden = !c.error;
    const approvals = d.querySelector("[data-approvals]");
    const approvalHTML = (c.approvals || []).map(a => I18n.html`<section class="asset-approval"><b>操作审批：${esc(a.action)}</b><p>${esc(a.purpose || a.detail)}</p><pre>${esc(a.detail)}</pre><button class="btn sm" data-approval="${esc(a.approval_id)}" data-allow="true">允许本次操作</button> <button class="btn sm" data-approval="${esc(a.approval_id)}" data-allow="false">拒绝</button></section>`).join("");
    if (approvals._content !== approvalHTML) {
      approvals.innerHTML = approvalHTML; approvals._content = approvalHTML;
      approvals.querySelectorAll("[data-approval]").forEach(button => button.onclick = async () => {
        const g = generation, id = selected;
        button.disabled = true;
        try { await api(`/api/asset-campaigns/${encodeURIComponent(id)}/approvals/${encodeURIComponent(button.dataset.approval)}`, {method: "POST", body: JSON.stringify({allow: button.dataset.allow === "true"})}); if (valid(g) && selected === id) await load(); }
        catch (e) { if (valid(g) && selected === id) { button.disabled = false; toast(e.message, true); } }
      });
    }
    const revision = `${Number(c.stats?.candidates || 0)}:${Number(c.stats?.assets_revision || 0)}`;
    if (resultsCount !== revision) { resultsCount = revision; loadResults(selected); }
    if (!source && streamRun !== c.started_at) connect(c);
  }
  function connect(c) {
    closeStream();
    const g = generation, id = selected;
    streamRun = c.started_at;
    const log = root().querySelector("[data-output]");
    if (!log.children.length) log.textContent = "";
    const es = new EventSource(`/api/asset-campaigns/${encodeURIComponent(id)}/events?after=${cursor}`);
    source = es;
    const current = () => valid(g) && selected === id && source === es;
    const connection = text => { if (current()) root().querySelector("[data-connection]").textContent = text; };
    connection(I18n.t("正在连接输出…"));
    es.onopen = () => connection(I18n.t("实时连接"));
    es.onerror = () => connection(I18n.t("输出连接中断，正在重连…"));
    es.addEventListener("output", e => {
      if (!current()) return;
      const row = JSON.parse(e.data);
      if (row.seq <= cursor) return;
      cursor = row.seq;
      appendOutput(row);
    });
    es.addEventListener("state", e => {
      if (!current()) return;
      const state = JSON.parse(e.data);
      const existing = campaigns.find(item => item.campaign_id === id);
      if (existing && existing.updated_at > state.updated_at) return;
      streamRun = state.started_at;
      campaigns = campaigns.map(item => item.campaign_id === id ? state : item);
      render(); updateDetail(state);
    });
    es.addEventListener("done", () => {
      if (!current()) return;
      connection(I18n.t("输出已同步")); closeStream(); loadResults(id);
      finishCommandBlocks(campaigns.find(item => item.campaign_id === id)?.status);
      const log = root().querySelector("[data-output]");
      if (!log.children.length) log.textContent = I18n.t("此历史任务未记录过程输出；重新运行后可查看实时日志。");
    });
    es.addEventListener("deleted", () => { if (current()) { clearDetail(); load().catch(() => {}); } });
  }
  async function loadResults(id) {
    const g = generation, req = ++resultsRequest;
    try {
      const data = await api(`/api/asset-campaigns/${encodeURIComponent(id)}/targets`);
      if (!valid(g) || selected !== id || req !== resultsRequest) return;
      const d = root().querySelector("[data-detail]");
      d.querySelector("[data-excluded]").textContent = I18n.t`本批次排除项：${(data.excluded_assets || []).join("、") || I18n.t("无")}`;
      resultData = data.targets || []; renderResults();
    } catch (e) {
      if (valid(g) && selected === id && req === resultsRequest) root().querySelector("[data-results]").textContent = I18n.t`结果加载失败：${e.message}`;
    }
  }
  function renderResults() {
      const d = root().querySelector("[data-detail]");

      const types = {domain: I18n.t("域名"), ip: I18n.t("IP 地址"), url: "URL", host: I18n.t("主机"), service: I18n.t("服务"), endpoint: I18n.t("接口")};
      const verification = {passive: I18n.t("被动情报，未验证"), unverified: I18n.t("未验证"), dns_resolved: I18n.t("DNS 已解析"), http_responded: I18n.t("HTTP 已响应"), http_verified: I18n.t("HTTP 已验证（历史标记）"), verification_failed: I18n.t("验证失败")};
      const sources = {spiritscout_internal: I18n.t("内置收集器"), oneforall: "OneForAll", fofa: "FOFA", ct: I18n.t("证书透明度记录"), "dns-brute": I18n.t("DNS 子域枚举")};
      const live = resultData.filter(httpResponded).length;
      d.querySelector("[data-result-count]").textContent = I18n.t`${resultData.length} 个资产 · ${live} 个 HTTP 已响应`;
      const filtered = resultData.filter(item => resultFilter === "all" || (resultFilter === "http_responded" ? httpResponded(item) : resultFilter === "unverified"
        ? !["http_responded", "http_verified", "dns_resolved", "verification_failed"].includes(item.verification_status)
        : item.verification_status === resultFilter));
      const pages = Math.max(1, Math.ceil(filtered.length / pageSize));
      resultPage = Math.max(1, Math.min(resultPage, pages));
      const start = (resultPage - 1) * pageSize, targets = filtered.slice(start, start + pageSize);
      d.querySelector("[data-page-info]").textContent = I18n.t`${filtered.length ? start + 1 : 0}–${Math.min(start + pageSize, filtered.length)} / ${filtered.length} · 第 ${resultPage} / ${pages} 页`;
      d.querySelector("[data-page-prev]").disabled = resultPage <= 1;
      d.querySelector("[data-page-next]").disabled = resultPage >= pages;
      d.querySelector("[data-page-size]").value = String(pageSize);
      d.querySelector("[data-result-filter]").value = resultFilter;
      if (typeof selxSync === "function") { selxSync(d.querySelector("[data-page-size]")); selxSync(d.querySelector("[data-result-filter]")); }
      d.querySelector("[data-results]").innerHTML = targets.length ? I18n.html`<table class="asset-result-table"><thead><tr><th>资产</th><th>类型</th><th>IP / CNAME</th><th>来源</th><th>验证状态</th></tr></thead><tbody>${targets.map(item => {
        const origin = item.source?.startsWith("asset-campaign:") ? I18n.t("历史收集（来源未记录）") : sources[item.source] || item.source || I18n.t("来源未记录");
        return I18n.html`<tr><td><code>${esc(item.value)}</code>${item.title ? `<small>${esc(item.title)}</small>` : ""}</td><td>${esc(types[item.asset_type] || item.asset_type || I18n.t("未知"))}</td><td>${esc(item.ip || I18n.t("未提供解析信息"))}${item.cname ? `<small>CNAME: ${esc(item.cname)}</small>` : ""}</td><td>${esc(origin)}${item.collection_method ? `<small>${esc(sources[item.collection_method] || item.collection_method)}</small>` : ""}</td><td>${esc(verification[item.verification_status] || item.verification_status || I18n.t("未记录"))}${item.http_status ? `<small>HTTP ${esc(item.http_status)}</small>` : ""}${item.http_error ? `<small>${esc(item.http_error)}</small>` : ""}${(item.http_observations || []).map(o => I18n.html`<small>${esc(o.egress_name || I18n.t("出口未知"))}${o.exit_ip ? ` · ${esc(o.exit_ip)}` : ""}：${o.http_status ? `HTTP ${esc(o.http_status)}` : esc(o.http_error || I18n.t("未获得响应"))}</small>`).join("")}</td></tr>`;
      }).join("")}</tbody></table>` : resultData.length ? I18n.html("当前筛选条件下没有资产。") : I18n.html("尚无资产；可先查看上方进展和失败原因。");
  }
  async function action(id, op) {
    if (busy.has(id)) return;
    const existing = campaigns.find(c => c.campaign_id === id);
    const needsModel = ["start", "resume"].includes(op) && existing?.status !== "paused";
    if (needsModel && !llm) { toast(I18n.t("请先选择资产收集 LLM 模型"), true); return; }
    const g = generation; busy.add(id); render();
    try {
      const c = await api(`/api/asset-campaigns/${encodeURIComponent(id)}/${op}`, {method: "POST", ...(needsModel ? {body: JSON.stringify({model: llm})} : {})});
      if (!valid(g) || !campaigns.some(item => item.campaign_id === id)) return;
      campaigns = campaigns.map(item => item.campaign_id === id ? c : item);
      if (selected !== id) selectCampaign(id);
      else { if (!terminal(c)) { closeStream(); streamRun = undefined; } updateDetail(c); }
      await load();
    } catch (e) { if (valid(g)) toast(e.message, true); }
    finally { busy.delete(id); if (valid(g)) render(); }
  }
  async function remove(id) {
    if (busy.has(id) || !window.confirm(I18n.t("删除该收集任务及过程输出？已写入资产库的结果会保留。"))) return;
    const g = generation; busy.add(id); render();
    try { await api(`/api/asset-campaigns/${encodeURIComponent(id)}`, {method: "DELETE"}); if (valid(g)) await load(); }
    catch (e) { if (valid(g)) toast(e.message, true); }
    finally { busy.delete(id); if (valid(g)) render(); }
  }
  function createDialog() {
    if (!project) { toast(I18n.t("请先选择项目"), true); return; }
    if (!llm) { toast(I18n.t("请先选择资产收集 LLM 模型"), true); return; }
    const projectId = project, selectedModel = llm, selectedProxy = proxyPlan, g = generation;
    const dlg = document.createElement("dialog"); dlg.className = "project-dlg asset-collection-dialog";
    dlg.innerHTML = I18n.html`<h3>新建资产收集任务</h3><p>本次 LLM：${esc(llmLabel(selectedModel))}</p><p>主动收集出口：${esc(proxyPlans.find(p => p.id === selectedProxy)?.name || I18n.html("直连"))}</p>${selectedProxy !== "direct" ? I18n.html('<label class="asset-fallback-option"><input type="checkbox" data-direct-fallback> 代理失败时允许直连复核（会暴露本机出口，仅复核失败目标）</label>') : ""}<div class="dlg-grid"><label class="wide"><span>目标资产或组织主体，每行一个</span><textarea data-targets rows="4" placeholder="example.com"></textarea></label><label class="wide"><span>本批次排除项，每行一个</span><textarea data-excluded rows="3"></textarea></label><label class="wide"><span>收集要求与特殊情况</span><textarea data-instruction rows="3" placeholder="说明需要收集什么、已知线索和限制，AI 会据此选择工具与编写命令"></textarea></label></div><p class="muted">AI 结合目标和项目规则选择命令、MCP 或收集引擎。结果不自动获得主动测试授权；危险动作即时审批。</p><div class="pj-error" role="alert"></div><div class="dlg-ops"><button class="btn" data-cancel>取消</button><button class="btn primary" data-ok>创建并启动</button></div>`;
    document.body.appendChild(dlg);
    let created = null;
    const closeDialog = () => { dlg.close(); dlg.remove(); };
    dlg.querySelector("[data-cancel]").onclick = closeDialog;
    dlg.addEventListener("cancel", e => { e.preventDefault(); closeDialog(); });
    dlg.querySelector("[data-ok]").onclick = async () => {
      const lines = key => dlg.querySelector(`[data-${key}]`).value.split(/\n|,/).map(x => x.trim()).filter(Boolean);
      const targets = lines("targets"), button = dlg.querySelector("[data-ok]");
      if (!targets.length) { dlg.querySelector(".pj-error").textContent = I18n.t("至少填写一个目标资产或组织主体。"); return; }
      button.disabled = true;
      try {
        // 启动失败时重试已有批次，不重复创建任务。
        if (!created) created = await api(`/api/projects/${encodeURIComponent(projectId)}/asset-campaigns`, {method: "POST", body: JSON.stringify({model: selectedModel, proxy_plan_id: selectedProxy, direct_fallback: !!dlg.querySelector("[data-direct-fallback]")?.checked, targets, excluded_assets: lines("excluded"), instruction: dlg.querySelector("[data-instruction]").value})});
        await api(`/api/asset-campaigns/${encodeURIComponent(created.campaign_id)}/start`, {method: "POST"});
        closeDialog();
        if (valid(g) && project === projectId) { await load(); selectCampaign(created.campaign_id); }
      } catch (e) { dlg.querySelector(".pj-error").textContent = e.message; }
      finally { button.disabled = false; }
    };
    dlg.showModal();
  }
  function open() {
    close(); active = true; project = ""; campaigns = []; selected = ""; cursor = 0; streamRun = undefined;
    try { llm = localStorage.getItem(modelPreference) || ""; } catch (_) { llm = ""; }
    models = [];
    shell(); load().catch(() => {});
    timer = setInterval(() => { if (active && !loading && S.view === "asset-collection" && document.visibilityState === "visible") load().catch(() => {}); }, 5000);
  }
  function close() {
    active = false; generation++; request++; resultsRequest++; loading = false;
    closeStream(); if (timer) clearInterval(timer); timer = null;
  }
  return {open, close};
})();
