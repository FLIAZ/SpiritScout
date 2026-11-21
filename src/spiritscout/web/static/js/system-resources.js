/* 只更新稳定节点，不重建浮层；页面隐藏时中止请求和轮询。 */
(() => {
  const trigger = document.getElementById('system-resource-toggle');
  const panel = document.getElementById('system-resource-panel');
  if (!trigger || !panel) return;
  const fields = new Map([...document.querySelectorAll('[data-resource]')]
    .map(node => [node.dataset.resource, node]));
  const bars = new Map([...document.querySelectorAll('[data-resource-bar]')]
    .map(node => [node.dataset.resourceBar, node]));
  const status = document.getElementById('resource-status');
  const updated = document.getElementById('resource-updated');
  const note = document.getElementById('resource-process-note');
  const empty = document.getElementById('resource-trend-empty');
  const cpuPath = document.getElementById('resource-trend-cpu');
  const memoryPath = document.getElementById('resource-trend-memory');
  const history = [];
  let lastSampleAt = null;
  let timer = null, request = null, generation = 0, suspended = false;
  const percent = value => Number.isFinite(value) ? `${value.toFixed(1)}%` : '—';
  function bytes(value) {
    if (!Number.isFinite(value)) return '—';
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
    return `${value.toFixed(unit ? 1 : 0)} ${units[unit]}`;
  }
  function text(key, value) {
    const node = fields.get(key);
    if (node.textContent !== value) node.textContent = value;
  }
  function meter(key, value) {
    const node = bars.get(key);
    node.style.transform = `scaleX(${Number.isFinite(value) ? Math.max(0, Math.min(1, value / 100)) : 0})`;
  }
  function state(value, message) {
    trigger.dataset.state = panel.dataset.state = value;
    if (status.textContent !== message) status.textContent = message;
    trigger.title = I18n.t('系统资源') + ' · ' + message;
  }
  function line(key, end) {
    let connected = false;
    return history.map(sample => {
      const value = sample[key];
      if (!Number.isFinite(value)) { connected = false; return ''; }
      const x = ((sample.at - (end - 300)) / 300 * 288).toFixed(2);
      const y = (66 - Math.max(0, Math.min(100, value)) / 100 * 58).toFixed(2);
      const command = connected ? 'L' : 'M';
      connected = true;
      return `${command}${x},${y}`;
    }).join(' ');
  }
  function trend(data) {
    const at = data.sampled_at;
    if (!Number.isFinite(at)) return;
    if (lastSampleAt === at) return; // 多标签页共享的同一快照不重复记点。
    if (lastSampleAt !== null && at < lastSampleAt) history.length = 0;
    lastSampleAt = at;
    history.push({at, cpu: data.system.cpu_percent, memory: data.system.memory_percent});
    while (history.length > 61 || history[0].at < at - 300) history.shift();
    cpuPath.setAttribute('d', line('cpu', at));
    memoryPath.setAttribute('d', line('memory', at));
    empty.hidden = history.length >= 2;
  }
  function render(data) {
    if (!data.available || !data.system) throw new Error(I18n.t('资源采样暂不可用'));
    const host = data.system, app = data.application;
    for (const key of ['summary-cpu', 'host-cpu']) {
      text(key, percent(host.cpu_percent)); meter(key, host.cpu_percent);
    }
    for (const key of ['summary-memory', 'host-memory']) {
      text(key, percent(host.memory_percent)); meter(key, host.memory_percent);
    }
    text('host-memory-bytes', `${bytes(host.memory_used)} / ${bytes(host.memory_total)}`);
    text('host-available', bytes(host.memory_available));
    text('app-cpu', percent(app?.cpu_percent));
    text('app-memory', bytes(app?.memory_rss));
    text('process-count', app ? I18n.t`${app.process_count} 个进程` : I18n.t('暂不可用'));
    note.textContent = I18n.t(!app ? '灵枢进程数据暂不可用。' : app.partial
      ? '部分进程无法读取，当前占用可能不完整。'
      : '包含当前服务及存活子进程，共享内存页可能重复计入。');
    state('live', I18n.t(host.cpu_percent === null ? 'CPU 采样中 · 请稍候' : '实时采样'));
    updated.dateTime = new Date(data.sampled_at * 1000).toISOString();
    updated.textContent = new Date(data.sampled_at * 1000).toLocaleTimeString(I18n.dateLocale,
      {hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
    trend(data);
  }
  function gap() {
    // 失败或隐藏期间不以直线伪装连续采样，也不把缺失值补成 0。
    const last = history.at(-1);
    if (last && (last.cpu !== null || last.memory !== null)) {
      history.push({at: last.at + .001, cpu: null, memory: null});
    }
  }
  async function poll() {
    if (document.hidden || suspended) return;
    const ownGeneration = generation;
    const controller = new AbortController();
    request = controller;
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const data = await api('/api/system/resources', {signal: controller.signal, cache: 'no-store'});
      if (generation === ownGeneration && !document.hidden && !suspended) render(data);
    } catch (error) {
      if (generation === ownGeneration && !document.hidden && !suspended) {
        gap();
        state('stale', I18n.t(updated.dateTime ? '采样中断 · 显示上次数据' : '资源采样暂不可用'));
      }
    } finally {
      clearTimeout(timeout);
      if (request === controller) request = null;
      if (generation === ownGeneration && !document.hidden && !suspended) timer = setTimeout(poll, 5000);
    }
  }
  function stop() {
    generation++;
    clearTimeout(timer);
    timer = null;
    request?.abort();
    request = null;
    gap();
    if (updated.dateTime) state('stale', I18n.t('等待恢复采样'));
  }
  document.addEventListener('visibilitychange', () => {
    stop();
    if (!document.hidden && !suspended) poll();
  });
  window.addEventListener('pagehide', () => { suspended = true; stop(); });
  window.addEventListener('pageshow', () => {
    if (!suspended) return;
    suspended = false;
    if (!document.hidden) poll();
  });
  panel.addEventListener('toggle', event => {
    trigger.setAttribute('aria-expanded', String(event.newState === 'open'));
  });
  poll();
})();
