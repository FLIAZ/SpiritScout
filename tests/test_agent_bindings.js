"use strict";
// 验证自动保存、批量部分失败和保存期间重入，不写入真实模型配置。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync("src/spiritscout/web/static/js/console.js", "utf8");
function setup(models = [{ key: "@A", model: "same", provider: "A" }, { key: "@B", model: "same", provider: "B" }]) {
  const selectors = ["master", "recon"].map(key => {
    const status = { textContent: "" };
    return { dataset: { agent: key, orig: key === "master" ? "@A" : "" },
      value: key === "master" ? "@A" : "", disabled: false,
      closest: () => ({ querySelector: () => status }), status };
  });
  const nodes = new Map();
  const $ = id => {
    if (!nodes.has(id)) nodes.set(id, { value: "", innerHTML: "", textContent: "", disabled: false,
      querySelectorAll: () => selectors });
    return nodes.get(id);
  };
  let assignments = { master: "@A" };
  const roster = [
    { key: "master", name: "灵枢", configurable: true, status: "available" },
    { key: "recon", name: "侦察", configurable: true, status: "available" },
    { key: "fixed", name: "无模型角色", configurable: false, status: "available" },
  ];
  const calls = [], errors = [];
  const state = () => ({ roster, models, assignments: { ...assignments } });
  const ctx = vm.createContext({ $, S: {}, esc: String, MODE_CN: {}, selxSync: () => {},
    toast: (...args) => errors.push(args),
    api: async (url, opts) => {
      if (!opts) return state();
      const model = JSON.parse(opts.body).model;
      const role = decodeURIComponent(url.split("/").pop());
      calls.push({ role, model });
      if (ctx.onPut) await ctx.onPut(role, model);
      if (model) assignments[role] = model;
      else delete assignments[role];
      return state();
    },
  });
  vm.runInContext(source.slice(0, source.indexOf("/* ================= 模型 ================= */")), ctx);
  return { ctx, $, selectors, calls, errors };
}
(async () => {
  const t = setup();
  await t.ctx.loadAgents();
  const [master, recon] = t.selectors;
  assert.equal(t.$("#bind-all-apply").disabled, true);
  master.value = "@B";
  const first = master.onchange();
  assert.equal(master.disabled, true, "保存期间禁用角色下拉");
  assert.equal(t.$("#bind-all-model").disabled, true, "保存期间禁用批量操作");
  await first;
  assert.deepEqual(t.calls, [{ role: "master", model: "@B" }], "修改后无需保存按钮即可提交供应商唯一 ID");
  assert.equal(master.dataset.orig, "@B");
  assert.equal(master.disabled, false);
  assert.equal(master.status.textContent, "已保存");
  recon.value = "@A";
  await recon.onchange();
  recon.value = "";
  await recon.onchange();
  assert.equal(t.ctx.S.agents.assignments.recon, undefined, "清除指派也自动保存");
  const before = t.calls.length;
  await recon.onchange();
  assert.equal(t.calls.length, before, "未改动不重复写入");

  t.ctx.onPut = async () => { throw new Error("网络断开"); };
  master.value = "@A";
  await master.onchange();
  assert.equal(master.value, "@B", "保存失败恢复原值");
  assert.equal(master.dataset.orig, "@B");
  assert.match(master.status.textContent, /保存失败/);
  assert.equal(t.errors.at(-1)[1], true);

  t.ctx.onPut = async role => { if (role === "master") throw new Error("拒绝写入"); };
  const all = t.$("#bind-all-model"), apply = t.$("#bind-all-apply");
  all.value = "@A";
  all.onchange();
  assert.equal(apply.disabled, false);
  const bulk = apply.onclick();
  await apply.onclick(); // 保存时重复点击不得再次提交。
  await bulk;
  assert.equal(master.value, "@B");
  assert.equal(recon.value, "@A", "批量个别失败不影响其余角色");
  assert.match(t.$("#bind-status").textContent, /已保存 1 个角色，1 个失败/);
  assert.equal(t.calls.some(call => call.role === "fixed"), false);
  t.ctx.onPut = null;
  await apply.onclick();
  assert.equal(master.value, "@A", "失败后可重试，只提交未应用的角色");
  assert.deepEqual(t.calls.at(-1), { role: "master", model: "@A" });
  assert.equal(apply.disabled, false);

  // 切页回来时等落盘完成再读配置，避免旧值重新出现在页面。
  let release;
  t.ctx.onPut = () => new Promise(resolve => { release = resolve; });
  master.value = "@B";
  const saving = master.onchange();
  await Promise.resolve();
  const loading = t.ctx.loadAgents();
  release();
  await saving;
  await loading;
  assert.equal(t.ctx.S.agents.assignments.master, "@B");

  const empty = setup([]);
  await empty.ctx.loadAgents();
  assert.equal(empty.$("#bind-all-model").disabled, true);
  assert.equal(empty.$("#bind-all-apply").disabled, true);
  console.log("agent bindings: autosave, clear, provider IDs, bulk apply, failure recovery, retry and reload passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
