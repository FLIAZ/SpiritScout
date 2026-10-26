"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const context = vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname,
  "../src/spiritscout/web/static/js/topology.js"), "utf8"), context);
const layout = vm.runInContext("TopologyLayout", context);
// 多层分支、汇合与共享祖先：每一条真实依赖都应指向更右一列。
const ids = ["entry", "task-a", "task-b", "route-a", "route-b", "asset-a", "asset-b", "note-a", "merge"];
const preds = new Map([
  ["entry", []], ["task-a", ["entry"]], ["task-b", ["entry"]],
  ["route-a", ["task-a"]], ["route-b", ["task-b"]],
  ["asset-a", ["route-a"]], ["asset-b", ["route-b"]],
  ["note-a", ["asset-a"]], ["merge", ["note-a", "asset-b"]]
]);
const graph = layout.layers(ids, preds);
for (const [id, parents] of preds) {
  for (const parent of parents) assert.ok(graph.depth.get(id) > graph.depth.get(parent), `${parent} → ${id}`);
}
assert.equal(graph.depth.get("asset-a"), 3);
assert.equal(graph.depth.get("note-a"), 4);
assert.equal(graph.depth.get("merge"), 5);
// 添加新分支不能把已有发现统一推到新的末列。
const appended = layout.layers([...ids, "late-task"], new Map([...preds, ["late-task", ["merge"]]]));
assert.equal(appended.depth.get("asset-a"), graph.depth.get("asset-a"));
// 不同高度的节点不重叠，后代跟随所属分支纵向排列。
const heights = new Map(ids.map(id => [id, id === "task-a" ? 230 : 70]));
const pos = layout.positions(graph.columns, preds, heights, 28, 44);
for (const column of graph.columns) {
  for (let i = 1; i < column.length; i++) {
    const before = column[i - 1], after = column[i];
    assert.ok(pos.y.get(after) >= pos.y.get(before) + heights.get(before) + 28);
  }
}
assert.ok(pos.y.get("asset-a") < pos.y.get("asset-b"));
assert.equal(pos.y.get("note-a"), pos.y.get("asset-a"));
// 历史坏数据及缺失依赖也能完成布局。
const cycle = layout.layers(["a", "b", "isolated"], new Map([
  ["a", ["b"]], ["b", ["a"]], ["isolated", ["missing"]]
]));
assert.equal(cycle.depth.size, 3);
assert.equal(cycle.depth.get("isolated"), 0);
console.log("topology: recursive branches, merges, incremental depth, height collisions and malformed data passed");
// 直接使用生产图构建代码，验证来源标识不会把同名路线串错。
const views = fs.readFileSync(path.join(__dirname,
  "../src/spiritscout/web/static/js/views.js"), "utf8");
vm.runInContext(views.slice(views.indexOf("function topoModel()"),
  views.indexOf("const nodeStatus")), context);
vm.runInContext(views.slice(views.indexOf("function topoReorder("),
  views.indexOf("function topoApplyView(")), context);
vm.runInContext(views.slice(views.indexOf("function drawTopo()"),
  views.indexOf("  const expanded = S.topoExpand")) +
  "return {nodes, layerIds, preds, routeMeta, depthIndex, dispFindings};}", context);
context.$ = () => ({});
context.TERMINAL = new Set(["completed"]);
context.S = {state: {session: {status: "completed"}, topology: {
  dag_nodes: [{node_id: "a", task_ids: [], round_created: 1},
    {node_id: "b", task_ids: ["task-b"], round_created: 1, depends_on: ["a"]}],
  tasks: [{task_id: "task-a", parent_dag_node: "a", agent_type: "recon",
    routes: [{route_id: "ra", name: "相同名称"}]},
  {task_id: "task-b", parent_dag_node: "b", agent_type: "recon",
    routes: [{route_id: "rb", name: "相同名称"}]}]
}, findings: [
  {finding_id: "fa", type: "note", text: "a 产出", task_id: "task-a", agent: "recon", route: "相同名称"},
  {finding_id: "fb", type: "note", text: "b 产出", task_id: "task-b", agent: "recon", route: "相同名称"},
  {finding_id: "fc", type: "note", text: "进一步产出", parent_finding_id: "fb"},
  {finding_id: "old", type: "note", text: "旧记录不清楚出处", agent: "recon", route: "相同名称", round: 1}
]}};
const built = vm.runInContext("drawTopo()", context);
assert.equal(built.routeMeta.size, 2, "parent_dag_node 归属任务也必须展示路线");
const routeFor = task => [...built.routeMeta].find(([, r]) => r.task_id === task)[0];
assert.equal(built.preds.get("__finding_fa")[0], routeFor("task-a"));
assert.equal(built.preds.get("__finding_fb")[0], routeFor("task-b"));
assert.equal(built.preds.get("__finding_old").length, 0, "歧义出处不能伪造连线");
assert.equal(built.depthIndex.get("__finding_fa"), 2);
assert.equal(built.depthIndex.get("__finding_fb"), 3);
assert.equal(built.depthIndex.get("__finding_fc"), 4);
console.log("topology: production graph task ownership, same-name routes, ambiguous provenance and grandchildren passed");
