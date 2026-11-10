"use strict";
/* 节点管理不改变运行中会话的固定出口。凭据仅在提交时发送。 */
const ProxyPoolPage = (() => {
  let generation = 0, selectorGeneration = 0, data = null, busy = false;
  let selected = [], source = 'all', tab = 'nodes', query = '', protocol = '';
  let benchmarkJob = null, benchmarkStarting = false, pollTimer = null;
  const root = () => document.querySelector('#view-proxy');
  const find = selector => root().querySelector(selector);
  const real = () => data.nodes.filter(n => n.selectable !== false);
  const label = id => data.nodes.find(n => n.id === id)?.name || '已移除节点';
  const sourceName = id => ({all:'全部节点', manual:'手动导入', legacy:'历史保留'})[id] || data.subscriptions.find(s => s.id === id)?.name || '历史保留';
  const visibleNodes = () => real().filter(n => (source === 'all' || n.source_id === source) && (!protocol || n.type === protocol) && `${n.name} ${n.server} ${n.type}`.toLowerCase().includes(query.toLowerCase()));
  const benchmarkActive = id => ['queued','testing'].includes(benchmarkJob?.results?.[id]?.status);
  const formatBytes = value => {
    if (!Number.isFinite(value)) return '—';
    const units = ['B','KB','MB','GB','TB','PB']; let size = Math.max(0, value), unit = 0;
    while (size >= 1024 && unit < units.length - 1) {size /= 1024; unit++;}
    return `${size >= 100 || unit === 0 ? size.toFixed(0) : size >= 10 ? size.toFixed(1) : size.toFixed(2)} ${units[unit]}`;
  };
  const usageLabel = usage => usage?.total ? `剩余 ${formatBytes(usage.remaining)} / ${formatBytes(usage.total)}` : '服务商未提供流量额度';
  function mergeBenchmarkResults(job) {
    if (!job || !data) return;
    for (const [id, result] of Object.entries(job.results || {})) {
      if (['queued','testing','cancelled','stale'].includes(result.status)) continue;
      const node = data.nodes.find(item => item.id === id);
      if (node) node.benchmark = result;
    }
  }
  function renderMeasurements() {
    const running = benchmarkStarting || benchmarkJob?.status === 'running';
    root().querySelectorAll('[data-measurement]').forEach(el => {
      const node = data.nodes.find(n => n.id === el.dataset.measurement);
      const current = benchmarkJob?.results[node.id];
      const loading = current && ['queued','testing'].includes(current.status);
      const result = current && !['queued','testing','cancelled'].includes(current.status) ? current : node.benchmark;
      const latency = Number.isFinite(result?.latency_ms) ? `${result.latency_ms.toFixed(0)} ms` : '— ms';
      const speed = Number.isFinite(result?.speed_mb_s) ? (result.speed_mb_s < .01 ? '<0.01' : result.speed_mb_s.toFixed(2)) + ' MB/s' : '— MB/s';
      const status = loading ? ({queued:'等待测速',testing:'测速中…'})[current.status] : ({available:'可达',unavailable:'请求失败',timeout:'连接超时',error:'测试失败',stale:'配置已变化'})[result?.status] || (current?.status === 'cancelled' ? '已停止' : '尚未测速');
      const time = result?.tested_at ? new Date(result.tested_at * 1000).toLocaleTimeString() : '';
      const detail = result?.error || result?.speed_error || `${result?.sample_count || 0}/3 次连接成功`;
      const speedTime = result?.speed_tested_at ? new Date(result.speed_tested_at * 1000).toLocaleString() : '';
      const content = `<div class="pool-measure-values"><strong class="${result?.status === 'available' ? 'is-ok' : result ? 'is-bad' : ''}" title="三次真实 HTTP 新连接请求的中位数，含代理握手、目标 TLS 和响应">${latency}</strong><strong title="${esc(`端到端实测：下载源 → 节点 → 本机；实际下载字节 / 请求总耗时（1 MB = 1,000,000 字节）${speedTime ? '；下载测试：' + speedTime : ''}`)}">${speed}</strong></div><small title="${esc(detail)}">${esc(status)}${time ? ' · ' + esc(time) : ''}${result?.speed_error && result?.status === 'available' ? ' · 下载采样受限' : ''}</small>`;
      if (el.innerHTML !== content) el.innerHTML = content;
    });
    root().querySelectorAll('[data-test-mode]').forEach(button => {
      const activeNode = button.dataset.testNode && benchmarkActive(button.dataset.testNode);
      const incompatible = benchmarkJob?.status === 'running' && (benchmarkJob.mode !== 'latency' || button.dataset.testMode !== 'latency');
      button.disabled = Boolean(!data.mihomo_available || activeNode || incompatible);
    });
    const stop = find('[data-stop-test]'); if (stop) stop.hidden = !running;
    const status = find('[data-benchmark-status]');
    if (status) {
      const results = Object.values(benchmarkJob?.results || {}), done = results.filter(r => !['queued','testing'].includes(r.status)).length;
      status.textContent = benchmarkJob ? `${({running:'测速中',completed:'测速完成',cancelled:'已停止',error:'测速出错'})[benchmarkJob.status]} · ${done}/${results.length}${benchmarkJob.mode === 'download' ? ' · 每节点最多下载 10 MB' : ''}` : '延迟：三次新连接请求中位数 · 网速：下载源 → 节点 → 本机的端到端速度';
    }
  }
  async function pollBenchmarks(token) {
    clearTimeout(pollTimer);
    try {
      const result = await api('/api/proxies/benchmark');
      if (token !== generation || !data) return;
      benchmarkJob = result.job; mergeBenchmarkResults(benchmarkJob); renderMeasurements();
      if (benchmarkJob?.status === 'running') pollTimer = setTimeout(() => pollBenchmarks(token), 1000);
      else if (benchmarkJob) {
        const refreshed = await api('/api/proxies');
        if (token !== generation || !data) return;
        data = refreshed; render();
      }
    } catch(error) {if (token === generation) message('测速状态读取失败：' + error.message, true);}
  }
  async function startTest(ids, mode) {
    const running = benchmarkJob?.status === 'running';
    if (!ids.length || benchmarkStarting || (running && (benchmarkJob.mode !== 'latency' || mode !== 'latency'))) return;
    ids = ids.filter(id => !benchmarkActive(id));
    if (!ids.length) return;
    const token = generation; benchmarkStarting = true; renderMeasurements();
    try {
      const result = await api('/api/proxies/benchmark', {method:'POST',body:JSON.stringify({nodes:ids,mode,version:data.version})});
      if (token !== generation) return;
      benchmarkJob = result.job; message(`已启动 ${ids.length} 个节点测速。测试使用独立出口，不改变会话代理。`);
      pollBenchmarks(token);
    } catch(error) {if (token === generation) message(error.message, true);}
    finally {if (token === generation) {benchmarkStarting = false; renderMeasurements();}}
  }
  function message(text, error = false) {const el = find('[data-pool-message]'); el.textContent = text; el.classList.toggle('is-error', error);}
  function bindDeletes() {root().querySelectorAll('[data-delete]').forEach(b => b.onclick = () => change('delete', {kind:b.dataset.kind,id:b.dataset.delete}));}
  function renderNodes() {
    const scoped = data.nodes.filter(n => source === 'all' || n.source_id === source);
    const nodes = scoped.filter(n => n.selectable !== false), notices = scoped.filter(n => n.selectable === false);
    const shown = nodes.filter(n => (!protocol || n.type === protocol) && `${n.name} ${n.server} ${n.type}`.toLowerCase().includes(query.toLowerCase()));
    find('[data-result-count]').textContent = `${shown.length} / ${nodes.length} 个节点`;
    find('[data-node-grid]').innerHTML = shown.map(n => {
      const i = selected.indexOf(n.id);
      return `<article class="pool-node ${i >= 0 ? 'is-selected' : ''}"><button class="pool-node-select" data-select="${esc(n.id)}" aria-pressed="${i >= 0}" aria-label="${esc(n.name)}，${i >= 0 ? '取消选择' : '选择节点'}"><span class="pool-node-top"><strong>${esc(n.name)}</strong><span class="pool-hop">${i >= 0 ? i + 1 : '+'}</span></span><span class="pool-endpoint">${esc(n.server)}:${n.port}</span><span class="pool-node-bottom"><b>${esc(n.type.toUpperCase())}</b><span>${n.tls_verification === 'disabled' ? 'TLS 校验关闭' : '会话出口未验证'}</span></span></button><div class="pool-measurement" data-measurement="${esc(n.id)}"></div><div class="pool-test-actions"><button class="btn sm" data-test-mode="latency" data-test-node="${esc(n.id)}">测延迟</button><button class="btn sm" data-test-mode="download" data-test-node="${esc(n.id)}" title="端到端下载最多 10 MB，包含延迟测试">测网速</button></div><button class="pool-node-delete" data-delete="${esc(n.id)}" data-kind="nodes" aria-label="删除 ${esc(n.name)}" title="删除节点">×</button></article>`;
    }).join('') || `<div class="pool-empty"><strong>${nodes.length ? '没有匹配的节点' : '这里还没有真实节点'}</strong><p>${nodes.length ? '换一个关键词或协议筛选。' : notices.length ? '服务商返回的是流量或客户端兼容提示。请刷新订阅，或获取 Mihomo 订阅链接。' : '添加订阅或导入配置后，节点会显示在这里。'}</p></div>`;
    find('[data-notices]').innerHTML = notices.length ? `<details class="pool-notices"><summary>订阅提示 · ${notices.length} 条（不可用作代理）</summary>${notices.map(n => `<p>${esc(n.name)}</p>`).join('')}</details>` : '';
    find('[data-node-grid]').querySelectorAll('[data-select]').forEach(b => b.onclick = () => {
      const id = b.dataset.select;
      if (selected.includes(id)) selected = selected.filter(key => key !== id);
      else if (selected.length < 8) selected.push(id);
      else {message('一条链路最多选择 8 个节点。', true); return;}
      renderNodes(); renderTray(); bindDeletes();
    });
    bindDeletes();
    find('[data-node-grid]').querySelectorAll('[data-test-node]').forEach(button => button.onclick = () => startTest([button.dataset.testNode], button.dataset.testMode));
    renderMeasurements();
  }
  function renderTray() {
    find('[data-chain-tray]').hidden = !selected.length;
    find('[data-chain-preview]').innerHTML = `<span>本机会话</span><i>→</i><span>录制网关</span>${selected.map((id,i) => `<i>→</i><button type="button" data-remove="${esc(id)}" title="移除此跳">${i+1}. ${esc(label(id))} ×</button>`).join('')}<i>→</i><span>目标</span>`;
    find('[data-chain-preview]').querySelectorAll('[data-remove]').forEach(b => b.onclick = () => {selected = selected.filter(id => id !== b.dataset.remove); renderNodes(); renderTray();});
  }
  function render() {
    selected = selected.filter(id => real().some(n => n.id === id));
    if (!['all','manual','legacy'].includes(source) && !data.subscriptions.some(s => s.id === source)) source = 'all';
    const groups = [{id:'all',name:'全部节点',node_count:real().length}, ...data.subscriptions];
    for (const id of ['manual','legacy']) {const count = real().filter(n => n.source_id === id).length; if (count) groups.push({id,name:sourceName(id),node_count:count});}
    find('[data-sources]').innerHTML = groups.map(s => `<button class="pool-source ${source === s.id ? 'is-active' : ''}" data-source="${esc(s.id)}" aria-pressed="${source === s.id}"><span>${esc(s.name)}</span><b>${s.node_count ?? 0}</b>${s.id !== 'all' && !['manual','legacy'].includes(s.id) ? `<small>${esc(usageLabel(s.usage))}</small>` : ''}${s.notice_count ? `<small>${s.notice_count} 条订阅提示</small>` : ''}</button>`).join('');
    find('[data-sources]').querySelectorAll('button').forEach(b => b.onclick = () => {source = b.dataset.source; render();});
    find('[data-pool-runtime]').textContent = data.mihomo_available ? 'Mihomo 已就绪 · 出口仍需验证' : '未安装 Mihomo · 可管理节点，复杂协议和多跳暂不可运行';
    find('[data-source-title]').textContent = sourceName(source);
    const sub = data.subscriptions.find(s => s.id === source);
    find('[data-source-info]').textContent = sub ? `${sub.node_count} 个节点${sub.updated_at ? ' · 更新于 ' + new Date(sub.updated_at * 1000).toLocaleString() : ' · 尚无刷新记录'}` : '选择节点，按点击顺序创建固定出口链路';
    find('[data-source-quota]').innerHTML = sub ? `<div class="pool-quota-summary"><div><span>剩余流量</span><strong>${sub.usage?.total ? esc(formatBytes(sub.usage.remaining)) : '—'}</strong></div><div><span>已用流量</span><strong>${sub.usage?.total ? esc(formatBytes(sub.usage.used)) : '—'}</strong></div><div><span>总流量</span><strong>${sub.usage?.total ? esc(formatBytes(sub.usage.total)) : '—'}</strong></div><div><span>到期时间</span><strong>${sub.usage?.expire ? esc(new Date(sub.usage.expire * 1000).toLocaleString()) : '未提供'}</strong></div></div>${sub.usage?.total ? `<div class="pool-quota-track" role="progressbar" aria-label="订阅剩余流量" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(Math.min(100, sub.usage.remaining / sub.usage.total * 100))}"><i style="width:${Math.min(100, sub.usage.remaining / sub.usage.total * 100)}%"></i></div>` : '<p class="pool-quota-empty">服务商响应未提供 subscription-userinfo，刷新节点仍可正常使用。</p>'}` : '';
    find('[data-source-actions]').innerHTML = sub ? `<button class="btn sm" data-refresh>更新配置</button><button class="btn sm" data-delete="${esc(sub.id)}" data-kind="subscriptions">移除订阅</button>` : '';
    find('[data-refresh]')?.addEventListener('click', () => change('refresh', {id:sub.id}));
    const warnings = data.subscriptions.filter(s => s.diagnostic && (source === 'all' || source === s.id));
    find('[data-pool-warning]').hidden = !warnings.length;
    find('[data-pool-warning]').textContent = warnings.map(s => `${s.name}：${s.diagnostic}`).join(' ');
    const types = [...new Set(real().map(n => n.type))].sort(); if (!types.includes(protocol)) protocol = '';
    find('[data-protocol]').innerHTML = '<option value="">全部协议</option>' + types.map(t => `<option value="${esc(t)}">${esc(t.toUpperCase())}</option>`).join(''); find('[data-protocol]').value = protocol;
    if (typeof selxSync === 'function') selxSync(find('[data-protocol]'));
    find('[data-node-panel]').hidden = tab !== 'nodes'; find('[data-plan-panel]').hidden = tab !== 'plans';
    root().querySelectorAll('[data-pool-tab]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.poolTab === tab)));
    renderNodes(); renderTray();
    find('[data-plan-panel]').innerHTML = data.plans.length ? data.plans.map(p => `<article class="pool-plan"><div><strong>${esc(p.name)}</strong><small>${p.nodes.length === 1 ? '固定单节点' : p.nodes.length + ' 跳固定链路'}</small></div><p class="pool-chain">本机会话 → 录制网关 → ${p.nodes.map(id => esc(label(id))).join(' → ')} → 目标</p><button class="btn sm" data-delete="${esc(p.id)}" data-kind="plans">删除方案</button></article>`).join('') : '<div class="pool-empty"><strong>还没有固定出口方案</strong><p>在节点页按顺序选择节点，再填写方案名称创建。新建 SRC 会话时可选择该方案。</p></div>';
    bindDeletes();
  }
  async function change(operation, payload) {
    if (busy || !data) return false;
    busy = true; const token = generation; root().setAttribute('aria-busy','true'); message(['subscribe','refresh'].includes(operation) ? (operation === 'refresh' ? '正在从远端更新订阅配置和流量额度…' : '正在获取订阅节点和流量额度…') : '正在保存…');
    try {
      const result = await api(`/api/proxies/${operation}`, {method:'POST',body:JSON.stringify({...payload,version:data.version})});
      if (token !== generation) return false; data = result;
      if (operation === 'subscribe') source = result.subscriptions.at(-1)?.id || 'all';
      if (operation === 'import') source = 'manual';
      if (operation === 'plan') {selected = []; tab = 'plans'; find('[data-plan]').reset();}
      if (benchmarkJob?.status !== 'running') benchmarkJob = null;
      render(); message(operation === 'refresh' ? '订阅配置、节点列表和流量额度已更新。运行中会话的固定出口不变。' : '已保存。运行中会话的固定出口不变。'); return true;
    } catch(error) {if (token === generation) message(error.message, true); return false;}
    finally {if (token === generation) {busy = false; root().removeAttribute('aria-busy');}}
  }
  function dialog(kind) {
    const token = generation, el = document.createElement('dialog'), importing = kind === 'import'; el.className = 'pool-dialog';
    el.innerHTML = `<form data-${kind}><header><h3>${importing ? '导入节点配置' : '添加订阅'}</h3><button type="button" class="btn sm" data-close aria-label="关闭">×</button></header>${importing ? '<label>选择配置文件<input type="file" data-config-file accept=".yaml,.yml,.txt,.conf"></label><label>Clash / Mihomo YAML 或节点列表<textarea name="content" required rows="9" autocomplete="off" spellcheck="false"></textarea></label><p>只导入内联节点。请移除脚本、远程 provider、TUN 和外部控制器配置。</p>' : '<label>订阅名称<input name="name" required maxlength="80" autocomplete="off" placeholder="例如：工作订阅"></label><label>HTTPS 订阅链接<input name="url" type="password" required autocomplete="off" maxlength="4096" placeholder="https://…"></label><p>使用 Mihomo 兼容格式获取节点。订阅地址加密保存，不在列表显示。</p>'}<p data-dialog-message role="status"></p><button class="btn" type="submit">${importing ? '导入节点' : '获取订阅节点'}</button></form>`;
    root().append(el); const form = el.querySelector('form');
    el.addEventListener('close', () => {form.reset(); el.remove();}); el.querySelector('[data-close]').onclick = () => el.close();
    el.querySelector('[data-config-file]')?.addEventListener('change', async event => {
      const file = event.target.files[0]; if (!file) return;
      if (file.size > 2*1024*1024) {el.querySelector('[data-dialog-message]').textContent = '配置文件超过 2 MiB'; return;}
      try {const content = await file.text(); if (token === generation && el.open) form.elements.content.value = content;}
      catch {if (el.open) el.querySelector('[data-dialog-message]').textContent = '读取文件失败，请重新选择。';}
    });
    form.onsubmit = async event => {
      event.preventDefault(); if (busy) return;
      const button = form.querySelector('[type="submit"]'); button.disabled = true; el.querySelector('[data-dialog-message]').textContent = '正在处理…';
      const ok = await change(kind, importing ? {content:form.elements.content.value} : {name:form.elements.name.value,url:form.elements.url.value});
      if (token !== generation || !el.isConnected) return;
      if (ok) el.close(); else {button.disabled = false; el.querySelector('[data-dialog-message]').textContent = find('[data-pool-message]').textContent;}
    }; el.showModal();
  }
  async function open() {
    const token = ++generation; data = null; busy = false; selected = []; source = 'all'; tab = 'nodes'; query = ''; protocol = ''; benchmarkJob = null; benchmarkStarting = false; clearTimeout(pollTimer);
    root().innerHTML = `<header class="pool-header"><div><span class="pool-eyebrow">出口控制台</span><h2>代理池</h2><p>管理订阅与节点，为 SRC 会话编排固定出口。</p></div><div class="pool-header-actions"><button class="btn" data-open-import disabled>导入配置</button><button class="btn primary" data-open-subscribe disabled>添加订阅</button></div></header><p data-pool-message role="status">正在读取代理池…</p><div class="pool-layout"><aside class="pool-rail"><h3>订阅与来源</h3><nav data-sources aria-label="节点来源"></nav><p data-pool-runtime class="pool-runtime"></p></aside><section class="pool-main"><header class="pool-source-header"><div><h3 data-source-title>全部节点</h3><p data-source-info></p></div><div data-source-actions></div></header><div data-source-quota></div><p class="pool-warning" data-pool-warning hidden></p><div class="pool-tabs" role="group" aria-label="代理池视图"><button data-pool-tab="nodes" aria-pressed="true">节点</button><button data-pool-tab="plans" aria-pressed="false">固定方案</button></div><div data-node-panel><div class="pool-toolbar"><input type="search" data-search aria-label="搜索节点" placeholder="搜索节点名称或服务器"><select data-protocol aria-label="筛选协议"><option value="">全部协议</option></select><span data-result-count></span></div><div class="pool-node-grid" data-node-grid></div><div data-notices></div></div><div data-plan-panel hidden></div></section></div><section class="pool-chain-tray" data-chain-tray hidden><div><strong>固定出口链路</strong><small>按选择顺序排列 · 最多 8 跳 · 点击移除</small></div><div class="pool-chain" data-chain-preview></div><form data-plan><label>方案名称<input name="name" required maxlength="80" placeholder="例如：SRC 固定出口"></label><button class="btn primary">创建固定方案</button></form></section>`;
    find('[data-open-import]').onclick = () => dialog('import'); find('[data-open-subscribe]').onclick = () => dialog('subscribe');
    find('[data-node-grid]').insertAdjacentHTML('beforebegin', '<div class="pool-benchmark-toolbar"><button class="btn sm" data-test-mode="latency" data-test-visible>测试全部延迟</button><button class="btn sm" data-test-mode="download" data-test-visible title="测试当前全部节点的端到端下载速度，每节点最多 10 MB">测试全部网速</button><button class="btn sm" data-stop-test hidden>停止测速</button><small data-benchmark-status role="status"></small></div>');
    root().querySelectorAll('[data-test-visible]').forEach(button => button.onclick = () => startTest(visibleNodes().map(n => n.id), button.dataset.testMode));
    find('[data-stop-test]').onclick = async () => {const stamp = generation; try {const result = await api('/api/proxies/benchmark/cancel', {method:'POST'}); if (stamp === generation) {benchmarkJob = result.job; renderMeasurements();}} catch(error) {if (stamp === generation) message(error.message, true);}};
    find('[data-search]').oninput = event => {query = event.target.value; if (data) renderNodes();};
    find('[data-protocol]').onchange = event => {protocol = event.target.value; if (data) renderNodes();};
    root().querySelectorAll('[data-pool-tab]').forEach(b => b.onclick = () => {if (data) {tab = b.dataset.poolTab; render();}});
    find('[data-plan]').onsubmit = event => {event.preventDefault(); change('plan', {name:event.target.elements.name.value,nodes:[...selected]});};
    try {const result = await api('/api/proxies'); if (token !== generation) return; data = result; render(); message(''); find('[data-open-import]').disabled = false; find('[data-open-subscribe]').disabled = false; pollBenchmarks(token);}
    catch(error) {if (token === generation) message(error.message, true);}
  }
  async function loadSelector(mode) {
    const token = ++selectorGeneration, option = document.querySelector('#c-proxy-option'), select = document.querySelector('#c-proxy-plan');
    if (!option || !select) return; option.hidden = mode !== 'src'; if (mode !== 'src') return;
    try {const result = await api('/api/proxies'); if (token !== selectorGeneration) return; const previous = select.value;
      select.innerHTML = '<option value="direct">直连</option>' + result.plans.map(p => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');
      if ([...select.options].some(item => item.value === previous)) select.value = previous;
      if (typeof selxRebuild === 'function' && select.closest('.selx')) selxRebuild(select);
      if (typeof selxSync === 'function') selxSync(select);
    } catch(error) {if (token === selectorGeneration) toast(error.message, true);}
  }
  return {loadSelector,navigate(view) {
    clearTimeout(pollTimer); root()?.querySelectorAll('dialog').forEach(el => el.close());
    if (view === 'proxy') open();
    else {
      ++generation; data = null; busy = false; root()?.replaceChildren();
      if (view === 'home') loadSelector(document.querySelector('#c-mode')?.value || '');
    }
  }};
})();
