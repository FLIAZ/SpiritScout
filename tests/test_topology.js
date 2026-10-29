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
// URL 与依据只占聚合卡片；事实依赖保留，重复引用不制造重复线。
const built = layout.project({nodes:[
  ...Array.from({length:100}, (_,i)=>({id:`url-${i}`,kind:"finding",layer:"asset",title:`https://test.local/${i}`})),
  {id:"evidence",kind:"evidence",title:"页面响应"},
  {id:"rule",kind:"rule",title:"规则与声明"},
  {id:"fact",kind:"finding",title:"普通成员不能访问管理接口"},
  {id:"derived",kind:"finding",title:"权限边界已观察"},
  {id:"shared",kind:"finding",title:"公共事实"},
  {id:"step",kind:"step",title:"执行步骤"}
], edges:[{source:"url-0",target:"fact"},{source:"url-1",target:"fact"},
  {source:"evidence",target:"fact"},{source:"fact",target:"derived"},
  {source:"shared",target:"fact"},{source:"shared",target:"derived"}]});
assert.equal(built.nodes.length, 6);
assert.equal(built.groups.find(n=>n.reference_type==="assets").members.length,100);
assert.equal(built.edges.find(e=>e.target==="fact" && e.source==="__process_group:assets").references.length,2);
assert.equal(built.nodes.find(n=>n.id==="shared").shared,true);
assert.ok(!built.nodes.some(n=>n.id==="step"));
assert.ok(built.edges.some(e=>e.source==="fact" && e.target==="derived"));
const many = Array.from({length:30},(_,i)=>`f${i}`);
const compact = layout.compact(many,new Map(many.map(id=>[id,[]])),new Map(many.map(id=>[id,100])),292,38,22,36,[]);
assert.ok(compact.height < 1500, "unrelated facts must wrap into a grid instead of a tall column");
for(let i=0;i<many.length;i++)for(let j=i+1;j<many.length;j++) {
  const a=many[i],b=many[j];
  assert.ok(Math.abs(compact.x.get(a)-compact.x.get(b))>=292 || Math.abs(compact.y.get(a)-compact.y.get(b))>=100);
}
const refsOnly=layout.project({nodes:[{id:"url",kind:"finding",title:"https://fixture.test"}],edges:[]});
assert.equal(refsOnly.nodes[0].kind,"collection");
assert.equal(refsOnly.records.length,1);
console.log("topology: compact fact layers, shared facts, grouped references and original source links passed");

const activity=layout.activity({tasks:[{task_id:'live',agent_type:'ctf_web',status:'running',routes:[{status:'running',note:'已读到脚本，正在核对提取结果'}]}]},[]);
assert.match(activity[0].text,/核对提取结果/);
const liveProgress=layout.activity({tasks:[{task_id:'live',status:'running'}]},[{task_id:'live',actor:'ctf-web-a',text:'已确认入口，准备验证访问条件'}]);
assert.match(liveProgress[0].text,/已确认入口/);
assert.equal(layout.project({nodes:[],edges:[]}).nodes.length,0,'progress is an overlay, never a synthetic fact node');
console.log('topology activity: live summaries, persisted-route fallback and fact separation passed');

const stages=Array.from({length:7},(_,i)=>({id:`stage-${i}`,kind:'milestone',task_id:'question',route_id:'web',turn:i+1,title:`阶段${i+1}`}));
const logic=layout.project({nodes:[...stages,{id:'result',kind:'finding',title:'有依据的结论'}],edges:[]});
assert.equal(logic.edges.filter(e=>e.kind==='progression').length,6);
assert.ok(logic.edges.every(e=>e.references.length===0),'chronology must not fabricate evidence');
const routeLayout=layout.compact(logic.nodes.map(n=>n.id),new Map(),new Map(logic.nodes.map(n=>[n.id,100])),292,38,22,36,[],stages);
assert.equal(routeLayout.y.get('stage-0'),routeLayout.y.get('stage-2'));
assert.ok(routeLayout.y.get('stage-3')>routeLayout.y.get('stage-2'));
assert.ok(routeLayout.x.get('stage-3')>routeLayout.x.get('stage-5'));
assert.ok(routeLayout.height<600,'route should wrap instead of creating a tall column');
console.log('logical routes: ordered stages, compact wrapping and explicit evidence separation passed');

const split=layout.dual({nodes:[
  {id:'asset',layer:'asset',kind:'finding',title:'login'},
  {id:'goal',kind:'exploration',node_type:'goal',task_id:'current',state:'open'},
  {id:'attempt',kind:'exploration',node_type:'attempt',task_id:'current',state:'refuted'},
  {id:'fact',kind:'finding',title:'403'}
],edges:[{source:'asset',target:'attempt',kind:'anchor'},
  {source:'goal',target:'attempt',kind:'derives'},{source:'attempt',target:'fact',kind:'produces'},
  {source:'missing',target:'fact',kind:'evidence'}]},[
  {id:'legacy',kind:'milestone',task_id:'old',route_id:'r',turn:1},
  {id:'duplicate-turn',kind:'milestone',task_id:'current',route_id:'r',turn:1}
]);
assert.equal(split.assets.nodes.length,1,'assets remain individual nodes');
assert.equal(split.exploration.nodes.length,4);
assert.equal(split.links.length,1);
assert.equal(split.exploration.edges.length,2);
assert.ok(split.exploration.nodes.some(n=>n.id==='attempt' && n.state==='refuted'));
assert.ok(!split.nodes.some(n=>n.id==='duplicate-turn'));
assert.equal(layout.dual({}).nodes.length,0);
console.log('dual graphs: explicit anchors, failed branches, missing edges and legacy fallback passed');

const boardIds=['root','a','b','leaf'], boardPreds=new Map([['root',[]],['a',['root']],['b',['root']],['leaf',['b']]]);
const boardHeights=new Map(boardIds.map(id=>[id,id==='a' ? 200 : 110]));
for(const viewport of [375,700]) {
  const board=layout.board(boardIds,boardPreds,boardHeights,248,viewport);
  assert.ok(board.width<=viewport);
  for(const [id,parents] of boardPreds) for(const parent of parents) assert.ok(board.y.get(id)>board.y.get(parent));
  for(let i=0;i<boardIds.length;i++) for(let j=i+1;j<boardIds.length;j++) {
    const a=boardIds[i],b=boardIds[j];
    assert.ok(Math.abs(board.x.get(a)-board.x.get(b))>=248 || board.y.get(a)+boardHeights.get(a)<=board.y.get(b)
      || board.y.get(b)+boardHeights.get(b)<=board.y.get(a),'variable-height nodes must not overlap');
  }
}
console.log('dual layout: viewport wrapping, dependency depth and variable-height collision checks passed');

const assetRelationships=layout.dual({nodes:[{id:'a',kind:'finding',layer:'asset'},
  {id:'b',kind:'finding',layer:'asset'},{id:'r',kind:'finding',relation:'exposes'}],
  edges:[{source:'a',target:'b',kind:'exposes',record_id:'relation-1'},
    {source:'a',target:'b',kind:'exposes',record_id:'relation-2'},{source:'a',target:'r',kind:'anchor'}]});
assert.equal(assetRelationships.assets.edges.length,1);
assert.equal(assetRelationships.assets.edges[0].references.length,2);
assert.equal(assetRelationships.exploration.nodes.length,0,'asset relationships are edges, not exploration stages');
assert.ok(assetRelationships.records.some(n=>n.id==='r'),'relationship provenance must remain available');
console.log('asset relationships: coalesced edges with complete source records passed');
