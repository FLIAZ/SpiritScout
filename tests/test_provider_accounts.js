'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const nodes = new Map();
const $ = id => {
  if (!nodes.has(id)) nodes.set(id, { value: '', hidden: false, offsetParent: {}, addEventListener() {} });
  return nodes.get(id);
};
const quotaNodes = [1, 2].map(() => ({ dataset: { providerQuota: 'same-account' }, innerHTML: '',
  classList: { toggle() {} }, setAttribute() {} }));
let resolveQuota, requests = 0;
const ctx = vm.createContext(require('./helpers/i18n').context({ $, S: { providers: [] }, Date, setTimeout, clearTimeout, setInterval() {},
  esc: x => String(x).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;'),
  modelKey: m => m.key,
  document: { hidden: false, querySelectorAll: selector => selector === '[data-provider-quota]' ? quotaNodes : [] },
  api: async () => { requests++; return new Promise(resolve => { resolveQuota = resolve; }); },
}));
vm.runInContext(fs.readFileSync('src/spiritscout/web/static/js/provider-accounts.js', 'utf8') + '\nglobalThis.accounts = ProviderAccounts;', ctx);
const a = ctx.accounts;
assert.match(a.quotaHTML({ status: 'ok', balances: [{ remaining: 0, currency: 'CNY' }] }), /quota-low/);
assert.doesNotMatch(a.quotaHTML({ status: 'unavailable', balances: [{ remaining: null, currency: 'CNY' }] }), /0 CNY/);
assert.match(a.quotaHTML({ status: 'unsupported' }), /暂不支持查询/);
assert.match(a.quotaHTML({ status: 'unavailable', stale: true, balances: [{ remaining: 42, currency: 'CNY' }] }), /上次结果/);
assert.doesNotMatch(a.quotaHTML({ status: 'ok', windows: [{ label: '<img src=x>', remaining_percent: 75 }] }), /<img/);
assert.equal(a.countdown('invalid'), '');
assert.equal(a.countdown('2000-01-01T00:00:00Z'), '待刷新');
assert.match(a.modelUsage({ key: '@provider-model' }), /data-model-usage="@provider-model"/);
(async () => {
  const first = a.refresh('same-account');
  await a.refresh('same-account');
  assert.equal(requests, 1, 'two model rows must share one quota request');
  resolveQuota({ status: 'ok', balances: [{ remaining: 42, currency: 'CNY' }] });
  await first;
  assert.equal(quotaNodes[0].innerHTML, quotaNodes[1].innerHTML);
  assert.match(quotaNodes[0].innerHTML, /42 CNY/);
  const old = a.refresh('same-account');
  a.reset();
  resolveQuota({ status: 'ok', balances: [{ remaining: 999, currency: 'CNY' }] });
  await old;
  assert.doesNotMatch(quotaNodes[0].innerHTML, /999 CNY/, 'late response after account change must not overwrite current UI');
  console.log('provider accounts: known zero, unknown quota, escaping, countdown, shared refresh and stale response isolation passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
