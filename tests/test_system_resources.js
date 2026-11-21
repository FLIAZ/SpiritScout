/* 摘要显示、趋势缺口、取消/乱序和页面生命周期，不依赖真实计时。 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('src/spiritscout/web/static/js/system-resources.js', 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));
function fixture(hidden = false) {
  const nodes = new Map(), events = {}, windowEvents = {}, timers = new Map(), requests = [];
  let nextTimer = 0;
  function node(id) {
    if (!nodes.has(id)) nodes.set(id, {dataset: {}, style: {}, textContent: '—', dateTime: '',
      hidden: false, handlers: {}, attributes: {},
      setAttribute(key, value) { this.attributes[key] = value; },
      addEventListener(key, fn) { this.handlers[key] = fn; }});
    return nodes.get(id);
  }
  const fields = ['summary-cpu', 'summary-memory', 'host-cpu', 'host-memory', 'host-memory-bytes',
    'host-available', 'app-cpu', 'app-memory', 'process-count'].map(key => {
      const n = node(key); n.dataset.resource = key; return n;
    });
  const bars = ['summary-cpu', 'summary-memory', 'host-cpu', 'host-memory'].map(key => {
    const n = node('bar-' + key); n.dataset.resourceBar = key; return n;
  });
  const document = {hidden, getElementById: node,
    querySelectorAll: selector => selector === '[data-resource]' ? fields : bars,
    addEventListener: (key, fn) => { events[key] = fn; }};
  vm.runInNewContext(source, require('./helpers/i18n').context({document,
    window: {addEventListener: (key, fn) => { windowEvents[key] = fn; }},
    AbortController, Date,
    setTimeout: (fn, delay) => { const id = ++nextTimer; timers.set(id, {fn, delay}); return id; },
    clearTimeout: id => timers.delete(id),
    api: (path, options) => new Promise((resolve, reject) => requests.push({path, options, resolve, reject}))}));
  return {node, document, requests, timers,
    visibility(value) { document.hidden = value; events.visibilitychange(); },
    hide() { windowEvents.pagehide(); }, show() { windowEvents.pageshow(); },
    tick(delay = 5000) {
      const [id, timer] = [...timers].find(([, value]) => value.delay === delay);
      timers.delete(id); timer.fn();
    }};
}
function sample(at, cpu = 25) {
  return {available: true, sampled_at: at,
    system: {cpu_percent: cpu, memory_percent: 60, memory_used: 6 * 1024 ** 3,
      memory_total: 10 * 1024 ** 3, memory_available: 4 * 1024 ** 3},
    application: {cpu_percent: 5, memory_rss: 128 * 1024 ** 2, process_count: 3, partial: false}};
}
(async () => {
  const f = fixture();
  assert.equal(f.requests[0].path, '/api/system/resources');
  assert.equal(f.requests[0].options.cache, 'no-store');
  f.requests[0].resolve(sample(1000, null)); await flush();
  assert.equal(f.node('summary-cpu').textContent, '—', '首个 CPU 样本不是伪造的 0');
  assert.equal(f.node('host-available').textContent, '4.0 GiB');
  assert.equal(f.node('app-memory').textContent, '128.0 MiB');
  assert.equal(f.node('process-count').textContent, '3 个进程');
  assert.equal(f.node('resource-trend-empty').hidden, false);
  assert.equal(f.node('bar-summary-memory').style.transform, 'scaleX(0.6)');
  f.tick(); f.requests[1].resolve(sample(1005)); await flush();
  assert.equal(f.node('summary-cpu').textContent, '25.0%');
  assert.equal(f.node('system-resource-toggle').dataset.state, 'live');
  assert.equal(f.node('resource-trend-empty').hidden, true);
  const stable = f.node('host-cpu');
  f.tick(); f.requests[2].reject(new Error('network')); await flush();
  assert.equal(f.node('system-resource-toggle').dataset.state, 'stale');
  assert.equal(stable.textContent, '25.0%', '失败保留上次数据并明确标为过期');
  f.tick(); f.requests[3].resolve(sample(1015)); await flush();
  assert.equal(f.node('host-cpu'), stable, '轮询不替换 DOM 节点');
  assert.equal((f.node('resource-trend-cpu').attributes.d.match(/M/g) || []).length, 2,
    '请求失败后的趋势必须断开，不能填 0 或连成连续采样');
  f.node('system-resource-panel').handlers.toggle({newState: 'open'});
  assert.equal(f.node('system-resource-toggle').attributes['aria-expanded'], 'true');
  f.node('system-resource-panel').handlers.toggle({newState: 'closed'});
  assert.equal(f.node('system-resource-toggle').attributes['aria-expanded'], 'false');

  const race = fixture();
  race.visibility(true);
  assert.equal(race.requests[0].options.signal.aborted, true);
  race.visibility(false);
  race.requests[1].resolve(sample(2000, 50)); await flush();
  race.requests[0].resolve(sample(1900, 90)); await flush();
  assert.equal(race.node('summary-cpu').textContent, '50.0%', '旧请求不能覆盖恢复后的新值');
  assert.equal([...race.timers.values()].filter(t => t.delay === 5000).length, 1);
  race.hide();
  assert.equal(race.timers.size, 0, 'pagehide 后不保留轮询');
  race.visibility(false);
  assert.equal(race.requests.length, 2, 'bfcache 暂停时 visibilitychange 不启动请求');
  race.show();
  assert.equal(race.requests.length, 3);
  race.requests[2].resolve(sample(2000, 50)); await flush();
  assert.equal(race.node('summary-cpu').textContent, '50.0%');

  const timed = fixture();
  timed.tick(8000);
  assert.equal(timed.requests[0].options.signal.aborted, true);
  timed.requests[0].reject(new Error('timeout')); await flush();
  assert.equal(timed.node('system-resource-toggle').dataset.state, 'stale');
  assert.equal(timed.node('summary-cpu').textContent, '—');
  timed.tick();
  timed.requests[1].resolve({...sample(3000), application: null}); await flush();
  assert.equal(timed.node('app-cpu').textContent, '—');
  assert.equal(timed.node('resource-process-note').textContent, '灵枢进程数据暂不可用。');
  const hidden = fixture(true);
  assert.equal(hidden.requests.length, 0, '隐藏页面不启动采样');
  console.log('system resource rendering, history, cancellation and lifecycle passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
