'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const html=fs.readFileSync(path.join(__dirname,'../src/spiritscout/web/static/index.html'),'utf8');
const core=fs.readFileSync(path.join(__dirname,'../src/spiritscout/web/static/js/core.js'),'utf8');
const page=fs.readFileSync(path.join(__dirname,'../src/spiritscout/web/static/js/asset-intelligence.js'),'utf8');

assert.match(html,/data-nav="asset-intelligence"/);
assert.match(html,/id="view-asset-intelligence"/);
assert.match(html,/asset-intelligence\.js/);
assert.match(core,/'asset-intelligence': '#view-asset-intelligence'/);
assert.match(core,/AssetIntelligencePage\.navigate\(view\)/);
assert.match(page,/\/api\/resources\/assets\/providers/);
assert.match(page,/FOFA/);
assert.match(page,/Hunter/);
assert.match(page,/Quake/);
assert.match(page,/ZoomEye/);
assert.match(page,/爱企查/);
assert.match(page,/企查查/);
assert.match(page,/type="password"/);
assert.match(page,/Cookie/);
assert.doesNotMatch(page,/localStorage|sessionStorage/);
assert.match(page,/expected_version:item\.version/);
assert.match(page,/clear_credentials:true/);
assert.match(page,/明确 URL、域名或子域名/);
assert.match(page,/公司、省份与控股线索/);
assert.doesNotMatch(page,/EnScan|enscan/);
console.log('asset intelligence page: provider center, native enterprise cookies, secret handling and scope rules passed');

// 批次页的行为契约：默认最新项目、请求身份隔离、输出回放和局部 DOM 保留。
(async () => {
  const vm = require('node:vm');
  class Element {
    constructor(tag = 'div') { this.tagName = tag; this.dataset = {}; this.children = []; this.nodes = new Map(); this.checked = true; this.scrollTop = 0; this.scrollHeight = 600; this.clientHeight = 200; this.classList = {toggle() {}}; }
    set innerHTML(value) { this.html = value; this.children = []; this.nodes.clear(); }
    get innerHTML() { return this.html || ''; }
    set textContent(value) { this.text = value; this.children = []; }
    get textContent() { return (this.text || '') + this.children.map(c => c.textContent).join(''); }
    querySelector(selector) {
      if (selector === '.resource-empty') return null;
      if (!this.nodes.has(selector)) { const node = new Element(); node.parent = this; this.nodes.set(selector, node); }
      return this.nodes.get(selector);
    }
    querySelectorAll(selector) {
      if (selector === '[data-campaign]') return this.children.filter(c => c.dataset.campaign);
      return [];
    }
    addEventListener() {}
    replaceChildren() { this.children = []; this.text = ''; }
    append(...nodes) { for (const node of nodes) { node.parent = this; this.children.push(node); } }
    appendChild(node) { this.append(node); }
    remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); }
  }
  const nodes = new Map(), requests = [], streams = [];
  const node = key => { if (!nodes.has(key)) nodes.set(key, new Element()); return nodes.get(key); };
  const root = new Element(); root.querySelector = node;
  node('[data-detail]').querySelector = node;
  let poll;
  class Events {
    constructor(url) { this.url = url; this.handlers = {}; streams.push(this); }
    addEventListener(name, fn) { this.handlers[name] = fn; }
    close() { this.closed = true; }
    emit(name, data) { this.handlers[name]?.({data: JSON.stringify(data)}); }
  }
  const preference = new Map();
  let modelCatalog = {models: [{key: "@one", model: "same", provider_ref: "OpenAI端点"}, {key: "@two", model: "same", provider_ref: "Anthropic端点"}]};
  const context = vm.createContext({...require('./helpers/i18n').context({}), document: {visibilityState: 'visible', createElement: tag => new Element(tag)},
    localStorage: {getItem: key => preference.get(key), setItem: (key, value) => preference.set(key, value)},
    $: () => root, S: {view: 'asset-collection'}, esc: x => String(x ?? ''), EventSource: Events,
    setInterval(fn) { poll = fn; return 1; }, clearInterval() { poll = null; }, toast() {},
    selxSync(select) { const options = [...select.innerHTML.matchAll(/value="([^"]*)">([^<]*)/g)]; select.visibleLabel = options.find(o => o[1] === select.value)?.[2] || ''; },
    api: (url, options) => url === "/api/proxies" ? Promise.resolve({plans: []}) : new Promise((resolve, reject) => requests.push({url, options, resolve, reject})),
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../src/spiritscout/web/static/js/asset-collection.js'), 'utf8'), context);
  const collection = vm.runInContext('AssetCollectionPage', context);
  const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
  const reply = async (url, data) => { if (url === "/api/projects") { const modelReq = requests.findIndex(r => r.url === "/api/models"); if (modelReq >= 0) requests.splice(modelReq, 1)[0].resolve(modelCatalog); } const index = requests.findIndex(r => r.url === url); assert.ok(index >= 0, `expected ${url}`); requests.splice(index, 1)[0].resolve(data); await flush(); };
  const catalog = {projects: [{project_id: 'old', name: '旧项目', created_at: 1}, {project_id: 'new', name: '最新项目', created_at: 10}]};
  const campaign = {campaign_id: 'batch-new', targets: ['example.test'], stats: {targets: 1, completed: 0, candidates: 0}, status: 'running', started_at: 10, updated_at: 10, created_at: 10};
  collection.open(); await reply('/api/projects', catalog);
  assert.equal(node('[data-project]').value, 'new'); assert.equal(node('[data-project]').visibleLabel, '最新项目');
  await reply('/api/projects/new/asset-campaigns', {campaigns: [campaign]});
  assert.equal(node('[data-model]').value, '', '不能默选首个模型或继承会话模型');
  assert.equal(node('[data-new]').disabled, true);
  assert.ok(node('[data-model]').innerHTML.includes('OpenAI端点 / same'));
  assert.ok(node('[data-model]').innerHTML.includes('Anthropic端点 / same'));
  node('[data-model]').onchange({target: {value: '@one'}});
  assert.equal(node('[data-new]').disabled, false);
  assert.equal(preference.get('spiritscout.asset-collection.model'), '@one');

  const stream = streams.at(-1), list = node('[data-list]'), card = list.children[0];
  const log = node('[data-output]');
  stream.emit('output', {seq: 1, message: '尚未结束的后台输出', level: 'info', created_at: 10});
  assert.equal(log.children.length, 1); assert.match(log.textContent, /尚未结束/);
  stream.emit('output', {seq: 1, message: '重复片段', created_at: 10}); assert.equal(log.children.length, 1);
  log.scrollTop = 42; node('[data-follow]').checked = false;
  stream.emit('output', {seq: 2, message: '下一条', level: 'info', created_at: 10}); assert.equal(log.scrollTop, 42);
  poll(); await reply('/api/projects', catalog); await reply('/api/projects/new/asset-campaigns', {campaigns: [campaign]});
  assert.equal(list.children[0], card, '未变化的卡片 DOM 不能被轮询重建'); assert.equal(log.scrollTop, 42);
  await reply('/api/asset-campaigns/batch-new/targets', {targets: []});
  stream.emit('state', {...campaign, updated_at: 12, stats: {targets: 1, completed: 1, candidates: 1}});
  assert.equal(log.children.length, 2);
  // 原始命令/输出只在默认折叠的执行块里；正文与滚动、展开状态保持稳定。
  stream.emit('output', {seq: 3, level: 'command', message: 'execution-1 · 核对公开入口', created_at: 10});
  const folded = log.children.at(-1), detailBody = folded.children[1];
  assert.equal(folded.tagName, 'details'); assert.notEqual(folded.open, true);
  assert.match(folded.children[0].textContent, /\[使用了命令\]/);
  stream.emit('output', {seq: 4, level: 'command', message: 'curl https://example.test', created_at: 10});
  stream.emit('output', {seq: 5, level: 'stdout', message: 'raw-output-secret-marker', created_at: 10});
  assert.match(detailBody.textContent, /raw-output-secret-marker/);
  assert.equal(log.children.filter(c => c.tagName !== 'details').some(c => c.textContent.includes('raw-output-secret-marker')), false);
  folded.open = true; detailBody.scrollTop = 30;
  stream.emit('output', {seq: 6, level: 'stderr', message: 'diagnostic', created_at: 10});
  stream.emit('output', {seq: 7, level: 'info', message: 'execution-1 · 退出码 0 · 执行完成', created_at: 10});
  stream.emit('output', {seq: 8, level: 'agent', message: '已核对入口存活', created_at: 10});
  assert.equal(folded.open, true); assert.equal(detailBody.scrollTop, 30);
  assert.match(log.children.at(-1).textContent, /已核对入口存活/);
  const beforeAssets = log.children.length;
  stream.emit('output', {seq: 9, level: 'asset', message: '新增资产：api-1.test', created_at: 10});
  stream.emit('output', {seq: 10, level: 'asset', message: '新增资产：api-2.test', created_at: 10});
  assert.equal(log.children.length, beforeAssets + 1, '批量资产登记不逐条刷屏');
  assert.match(log.children.at(-1).textContent, /api-2.test/);
  const usageState = {...campaign, updated_at: 13, stats: {candidates: 1, assets_revision: 1, usage: {
    used_tokens: 1500, cache: {total_input_tokens: 1200, output_tokens: 300, cached_input_tokens: 600,
      measured_calls: 2, unmeasured_calls: 1, hit_rate: .5}}}};
  stream.emit('state', usageState);
  assert.equal(node('[data-usage-total]').textContent, (1500).toLocaleString());
  assert.equal(node('[data-usage-rate]').textContent, '50.0%');
  assert.match(node('[data-usage-coverage]').textContent, /2 次已提供，1 次未提供/);
  const rows = Array.from({length: 32}, (_, i) => ({value: `api-${i}.example.test`, asset_type: 'domain',
    verification_status: i < 22 ? 'http_responded' : 'unverified', http_status: i < 22 ? 403 : null}));
  // 定向兑现两个正在排队的结果请求，后发的版本才写入页面。
  while (requests.some(r => r.url === '/api/asset-campaigns/batch-new/targets')) await reply('/api/asset-campaigns/batch-new/targets', {targets: rows});
  assert.equal((node('[data-results]').innerHTML.match(/<tr>/g) || []).length - 1, 15);
  node('[data-page-next]').onclick(); assert.match(node('[data-page-info]').textContent, /第 2 \/ 3 页/);
  node('[data-page-size]').onchange({target: {value: '10'}});
  assert.equal((node('[data-results]').innerHTML.match(/<tr>/g) || []).length - 1, 10);
  node('[data-result-filter]').onchange({target: {value: 'http_responded'}});
  node('[data-page-next]').onclick();
  stream.emit('state', {...usageState, updated_at: 14, stats: {...usageState.stats, assets_revision: 2}});
  await reply('/api/asset-campaigns/batch-new/targets', {targets: [...rows, {value:'new.example.test', verification_status:'http_responded', http_status:302}]});
  assert.match(node('[data-page-info]').textContent, /23 · 第 2 \/ 3 页/);
  assert.equal(node('[data-page-size]').value, '10');
  assert.equal(folded.open, true);
  stream.emit('state', {...usageState, updated_at: 15, stats: {...usageState.stats, assets_revision: 3, usage: {
    used_tokens: 1700, cache:{total_input_tokens:1400, output_tokens:300, measured_calls:0, unmeasured_calls:3, hit_rate:null}}}});
  assert.equal(node('[data-usage-rate]').textContent, '未提供');
  assert.equal(node('[data-usage-cached]').textContent, '未提供');
  // 旧项目请求、结果请求和 SSE 都不得覆盖新项目。
  const staleResults = requests.find(r => r.url.endsWith('/targets'));
  node('[data-project]').onchange({target: {value: 'old'}}); await reply('/api/projects', catalog);
  assert.equal(stream.closed, true); assert.equal(node('[data-project]').visibleLabel, '旧项目');
  const staleCampaign = requests.find(r => r.url === '/api/projects/old/asset-campaigns');
  node('[data-project]').onchange({target: {value: 'new'}}); await reply('/api/projects', catalog);
  staleCampaign.resolve({campaigns: [{campaign_id: 'batch-old', targets: ['wrong.test']}]});
  staleResults.resolve({targets: [{value: 'wrong.test'}]});
  stream.emit('output', {seq: 3, message: '旧流', created_at: 11}); await flush();
  assert.equal(list.children.length, 0, '项目切换必须立即清空旧任务');
  await reply('/api/projects/new/asset-campaigns', {campaigns: [campaign]});
  assert.match(list.children[0].innerHTML, /example.test/); assert.doesNotMatch(list.children[0].innerHTML, /wrong.test/);
  assert.equal(log.children.length, 0, '旧 SSE 不能写入新任务');
  const resumed = streams.at(-1);
  resumed.emit('output', {seq: 1, message: '回放输出', level: 'info', created_at: 10});
  resumed.emit('done', {}); assert.equal(resumed.closed, true);
  assert.equal(node('[data-connection]').textContent, '输出已同步');
  node('[data-project]').onchange({target: {value: 'old'}}); await reply('/api/projects', catalog);
  collection.close(); collection.open(); await reply('/api/projects', catalog);
  assert.equal(node('[data-project]').value, 'new', '重新进入默认最新项目，而不是上一次手选项目');
  collection.close(); assert.equal(poll, null);
  assert.equal(node('[data-model]').value, '@one', '重新进入恢复资产页自己的选择');
  collection.open(); modelCatalog = {models: [modelCatalog.models[1]]};
  await reply('/api/projects', catalog); await reply('/api/projects/new/asset-campaigns', {campaigns: []});
  assert.equal(node('[data-model]').value, '', '所选模型移除后不自动切到另一供应商');
  assert.equal(node('[data-new]').disabled, true);
  collection.close();
  console.log('asset collection: latest project, live output, replay deduplication, DOM preservation and stale response isolation passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
