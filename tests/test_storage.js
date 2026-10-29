"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const code = fs.readFileSync("src/spiritscout/web/static/js/storage.js", "utf8");
function fixture() {
  const nodes = new Map(), calls = [], timers = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {textContent:"", innerHTML:"", value:"", disabled:false,
      checked:true, open:false, classList:{toggle() {}}, handlers:{},
      addEventListener(name, fn) { this.handlers[name] = fn; },
      showModal() { this.open = true; },
      close() { this.open = false; this.handlers.close?.(); },
    });
    return nodes.get(id);
  };
  const ctx = vm.createContext({$:node, esc:String, selxSync() {},
    setTimeout:fn => { const n = timers.size + 1; timers.set(n, fn); return n; },
    clearTimeout:n => timers.delete(n),
    api: async (url, options) => {
      calls.push({url, options});
      if (ctx.handle) return ctx.handle(url, options);
      if (url === "/api/storage/sessions") return {total_bytes:100000, sessions:[
        {session_id:"s1", title:"会话一", bytes:80000}, {session_id:"s2", title:"会话二", bytes:20000}]};
      if (url.endsWith("/storage/job")) return ctx.job || {status:"idle"};
      if (options?.method === "POST") { ctx.job = {status:"running", phase:"正在收缩"}; return ctx.job; }
      return {bytes:80000, cache_bytes:2000, database:{bytes:70000, old_dag_versions:5, delivered_events:3},
        blocked_reason:ctx.blocked || ""};
    },
  });
  vm.runInContext(code, ctx);
  return {ctx, node, calls, timers, ui:vm.runInContext("SessionStorage", ctx)};
}
const flush = async () => { for (let n = 0; n < 12; n++) await Promise.resolve(); };
(async () => {
  const t = fixture();
  await t.ui.open("s1");
  assert.equal(t.node("#storage-dlg").open, true);
  assert.equal(t.node("#storage-clean").disabled, false);
  assert.equal(t.calls.some(c => c.options?.method === "POST"), false, "预览不能发起清理");
  assert.match(t.node("#storage-details").innerHTML, /旧 DAG 快照 5 个/);
  assert.equal(t.ui.bytes(1024 ** 3), "1.00 GiB");
  t.node("#storage-database").checked = false;
  t.node("#storage-caches").checked = false;
  t.node("#storage-database").onchange();
  assert.equal(t.node("#storage-clean").disabled, true);
  t.node("#storage-caches").checked = true;
  t.node("#storage-caches").onchange();
  await t.node("#storage-clean").onclick();
  await flush();
  const post = t.calls.find(c => c.options?.method === "POST");
  assert.deepEqual(JSON.parse(post.options.body), {confirm:true, database:false, caches:true});
  assert.equal(t.node("#storage-clean").disabled, true);
  assert.match(t.node("#storage-status").textContent, /正在收缩/);
  t.node("#storage-close").onclick();
  assert.equal(t.timers.size, 0, "关闭弹窗停止轮询但不发送取消请求");
  await t.ui.open("s1");
  assert.match(t.node("#storage-status").textContent, /正在收缩/, "再次打开可查看服务端进度");
  t.ctx.job = {status:"partial", released_bytes:1024, warnings:["空间不足，未收缩文件"]};
  const next = [...t.timers.values()][0];
  await next(); await flush();
  assert.match(t.node("#storage-status").textContent, /已释放 1.00 KiB/);
  assert.match(t.node("#storage-status").textContent, /空间不足/);
  assert.equal(t.node("#storage-clean").disabled, false);

  const blocked = fixture();
  blocked.ctx.blocked = "会话仍在执行";
  await blocked.ui.open("s1");
  assert.equal(blocked.node("#storage-clean").disabled, true);
  assert.match(blocked.node("#storage-status").textContent, /会话仍在执行/);
  await blocked.node("#storage-clean").onclick();
  assert.equal(blocked.calls.some(c => c.options?.method === "POST"), false);

  // 先打开的请求迟到，不能覆盖用户随后选择的会话。
  const stale = fixture();
  let release;
  const original = stale.ctx.api;
  stale.ctx.api = (url, opts) => url.includes("s1/storage") && !release
    ? new Promise(resolve => { release = resolve; }) : original(url, opts);
  const older = stale.ui.open("s1");
  await flush();
  const newer = stale.ui.open("s2");
  await newer;
  release({bytes:9999999, database:{}, blocked_reason:"stale"});
  await older;
  assert.equal(stale.node("#storage-select").value, "s2");
  assert.doesNotMatch(stale.node("#storage-status").textContent, /stale/);
  console.log("storage UI: preview-only, explicit cleanup, selection, polling, reopen, partial failure and stale responses passed");
})().catch(error => {console.error(error); process.exitCode = 1;});
