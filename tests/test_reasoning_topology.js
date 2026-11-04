'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const context=vm.createContext({});
vm.runInContext(fs.readFileSync('src/spiritscout/web/static/js/topology.js','utf8'),context);
const layout=vm.runInContext('TopologyLayout',context);
const state={session:{title:'验证测试环境的访问边界',target:'https://fixture.test',status:'running'},topology:{process:{nodes:[
  {id:'asset',kind:'finding',layer:'asset',title:'https://fixture.test/api'},
  {id:'rule',kind:'rule',reference_type:'rules',title:'用户规则'},
  {id:'response',kind:'evidence',reference_type:'evidence',title:'响应原文'},
  {id:'goal',kind:'exploration',node_type:'goal',title:'核对访问边界',task_id:'task'},
  {id:'hypothesis',kind:'exploration',node_type:'hypothesis',title:'替换用户标识可能改变数据归属',task_id:'task'},
  {id:'attempt',kind:'exploration',node_type:'attempt',title:'对照两个授权测试账号',task_id:'task'},
  {id:'fact',kind:'finding',title:'访客返回401',evidence_state:'observed',task_id:'task'},
  {id:'orphan',kind:'finding',title:'归属尚未记录的观察',evidence_state:'unverified'},
],edges:[{source:'goal',target:'hypothesis',kind:'derives'},{source:'hypothesis',target:'attempt',kind:'derives'},
  {source:'attempt',target:'fact',kind:'produces'},{source:'asset',target:'attempt',kind:'anchor'},
  {source:'response',target:'fact',kind:'evidence'}]},dag_nodes:[]},tasks:[{task_id:'task',title:'访问边界核对',status:'running'}]};
const original=JSON.stringify(state),graph=layout.reasoning(state,[{id:'stage',kind:'milestone',task_id:'task',turn:1,title:'公开进展'}]);
assert.equal(JSON.stringify(state),original,'projection must not mutate persisted data');
assert.equal(graph.nodes.filter(n=>n.view_kind==='origin').length,1);
assert.ok(!graph.nodes.some(n=>['asset','rule','response','stage'].includes(n.id)),'assets, resources and per-turn logs do not become reasoning nodes');
assert.equal(graph.assets.nodes.length,1);
assert.equal(graph.references.length,2);
assert.equal(graph.nodes.find(n=>n.id==='hypothesis').view_kind,'intent');
assert.equal(graph.nodes.find(n=>n.id==='fact').evidence_state,'observed');
assert.ok(graph.edges.some(e=>e.source==='attempt' && e.target==='fact' && e.kind==='produces'));
assert.ok(!graph.edges.some(e=>e.target==='orphan'),'unattributed facts must not receive invented causality');
assert.ok(!graph.edges.some(e=>e.kind==='progression'),'public chronology uses a distinct advance relation');
assert.ok(graph.records.some(n=>n.id==='asset') && graph.records.some(n=>n.id==='response'),'full reference records remain accessible');

const legacy=layout.reasoning({session:{title:'历史会话'},topology:{dag_nodes:[
  {node_id:'entry',title:'确认入口',task_ids:['t1'],depends_on:[]},
  {node_id:'auth',title:'核对权限',task_ids:['t2'],depends_on:['entry']}
],process:{nodes:[{id:'output',kind:'finding',title:'管理接口返回403',task_id:'t2',evidence_state:'unverified'}],edges:[]}},
  tasks:[{task_id:'t1',title:'读取入口',parent_dag_node:'entry',status:'done'},{task_id:'t2',title:'检查访问条件',parent_dag_node:'auth',status:'running'}]},[
  {id:'one',task_id:'t2',turn:1,title:'第一阶段',body:'公开说明一'},
  {id:'two',task_id:'t2',turn:2,title:'第二阶段',body:'公开说明二'}
]);
assert.equal(legacy.nodes.filter(n=>n.view_kind==='goal').length,2);
assert.equal(legacy.nodes.filter(n=>n.view_kind==='intent' && n.node_type!=='stage').length,2);
assert.equal(legacy.nodes.filter(n=>n.node_type==='stage').length,0,'execution rounds cannot become canvas nodes');
assert.ok(!legacy.edges.some(e=>e.kind==='advance'),'chronology alone cannot assert an intention change');
assert.equal(legacy.nodes.find(n=>n.id==='__process_intent:t2').progress.length,2);
assert.ok(legacy.edges.some(e=>e.source==='__process_goal:entry' && e.target==='__process_goal:auth' && e.kind==='dependency'));
assert.ok(legacy.edges.some(e=>e.source==='__process_intent:t2' && e.target==='output' && e.kind==='task_output'));
assert.equal(legacy.nodes.find(n=>n.id==='output').evidence_state,'unverified');

const replay=layout.reasoning({topology:{public_stages:{items:[
  {id:'old-1',task_id:'t',route_id:'r',turn:1,title:'入口被拒绝',body:'明确的公开说明',status:'done',exploration:{intent:'确认访问条件',attempt:'核对认证要求'}},
  {id:'old-2',task_id:'t',route_id:'r',turn:2,title:'继续核对认证要求',status:'done'},
  {id:'other',task_id:'t',route_id:'parallel',turn:1,title:'并行核对',status:'done',exploration:{intent:'确认访问条件',attempt:'核对认证要求'}}
]},process:{nodes:[{id:'answer',kind:'finding',title:'最终产出',provenance:{producer:{task_id:'t',route_id:'r',turn:2}},evidence_state:'unverified'}],edges:[]}}},[
  {id:'live-2',task_id:'t',route_id:'r',turn:2,title:'同一条实时记录',status:'running'}
]);
const replayAttempts=replay.nodes.filter(n=>n.view_kind==='attempt');
assert.equal(replayAttempts.length,2,'same attempt absorbs multiple rounds while parallel routes remain isolated');
const answerEdge=replay.edges.find(e=>e.target==='answer');
assert.equal(replay.nodes.find(n=>n.id===answerEdge.source).turns.includes(2),true,'output connects to the attempt containing its recorded producer step');
assert.equal(replay.nodes.find(n=>n.id===answerEdge.source).status,'done','stale chat snapshots do not regress persisted status');
assert.ok(!replay.edges.some(e=>e.kind==='redirects'),'parallel attempts do not acquire invented transition edges');

const namedTask=layout.reasoning({topology:{dag_nodes:[{node_id:'g',title:'同一个任务',task_ids:['t']}],
  public_stages:{items:[{task_id:'t',route_id:'r',turn:1,title:'明确的探索步骤'}]}},
  tasks:[{task_id:'t',title:'同一个任务',parent_dag_node:'g'}]});
assert.equal(namedTask.nodes.filter(n=>n.title==='同一个任务').length,1,'a single same-name DAG task does not create duplicate overview cards');
assert.equal(namedTask.nodes.find(n=>n.view_kind==='goal').progress.length,1);
assert.ok(!namedTask.nodes.some(n=>n.node_type==='stage'));

const attributed=layout.reasoning({topology:{process:{nodes:[
  {id:'intent',kind:'exploration',node_type:'attempt',provenance:{producer:{task_id:'t'}}},
  {id:'note',kind:'finding',layer:'recon_clue',provenance:{producer:{task_id:'t'}}}
],edges:[]}},tasks:[{task_id:'t',title:'已记录的探索'}]});
assert.equal(attributed.nodes.filter(n=>n.view_kind==='attempt').length,1,'producer attribution reuses the explicit intent');
assert.equal(attributed.nodes.find(n=>n.id==='note').view_kind,'hint');
assert.ok(attributed.edges.some(e=>e.source==='intent' && e.target==='note' && e.kind==='hint'));

const turns=Array.from({length:30},(_,i)=>({task_id:'t',route_id:'r',turn:i+1,title:'公开执行说明',status:'done',
  tool_call_ids:[`call-${i+1}`],tools:[{id:`call-${i+1}`,name:'http_request',status:'done'}]}));
turns[0].exploration={intent:'验证访问边界',attempt:'对照匿名请求',state:'refuted',reason:'现场响应否定该方法'};
turns[10].exploration={intent:'验证访问边界',attempt:'对照授权账号',from:'对照匿名请求',reason:'改用已授权的账号对照',state:'running'};
turns[29].exploration={intent:'验证访问边界',attempt:'对照授权账号',state:'closed'};
const hierarchy=layout.reasoning({topology:{public_stages:{items:turns},process:{nodes:[
  {id:'produced',kind:'finding',task_id:'t',route_id:'r',provenance:{producer:{turn:19}},evidence_state:'unverified'}],edges:[]}},
  tasks:[{task_id:'t',status:'completed'}]});
assert.equal(hierarchy.nodes.filter(n=>n.view_kind==='intent').length,1);
const attempts=hierarchy.nodes.filter(n=>n.view_kind==='attempt');
assert.equal(attempts.length,2,'thirty execution rounds form two declared methods, not thirty nodes');
assert.deepEqual(Array.from(attempts,n=>n.progress.length),[10,20]);
assert.equal(attempts[0].state,'refuted','failed methods remain visible');
assert.equal(attempts[1].state,'closed','same title updates the existing attempt');
assert.equal(attempts[1].tool_call_ids.length,20,'tool execution remains in attempt details');
assert.ok(hierarchy.edges.some(e=>e.kind==='redirects' && e.source===attempts[0].id && e.target===attempts[1].id));
assert.ok(hierarchy.edges.some(e=>e.source===attempts[1].id && e.target==='produced'));
assert.equal(hierarchy.nodes.find(n=>n.id==='produced').evidence_state,'unverified','attempt closure cannot verify a fact');
const sameNames=layout.reasoning({topology:{public_stages:{items:[
  {task_id:'t',route_id:'r',turn:1,exploration:{intent:'第一问题',attempt:'对照测试'}},
  {task_id:'t',route_id:'r',turn:2,exploration:{intent:'第二问题',attempt:'对照测试'}},
]}}});
assert.equal(sameNames.nodes.filter(n=>n.view_kind==='attempt').length,2,'different intentions cannot merge merely because method titles match');

const explicitAndPublic=layout.reasoning({topology:{process:{nodes:[
  {id:'explicit-intent',kind:'exploration',node_type:'hypothesis',task_id:'t',route_id:'r',title:'核对边界'},
  {id:'explicit-attempt',kind:'exploration',node_type:'attempt',task_id:'t',route_id:'r',title:'对照请求'}
],edges:[{source:'explicit-intent',target:'explicit-attempt',kind:'derives'}]},public_stages:{items:[
  {task_id:'t',route_id:'r',turn:1,exploration:{intent:'核对边界',attempt:'对照请求',state:'running'}},
  {task_id:'t',route_id:'r',turn:2,exploration:{intent:'核对边界',attempt:'对照请求',state:'closed'}}
]}}});
assert.equal(explicitAndPublic.nodes.filter(n=>n.view_kind==='intent').length,1,'matching explicit records and public labels share one intention');
assert.equal(explicitAndPublic.nodes.filter(n=>n.view_kind==='attempt').length,1);
assert.equal(explicitAndPublic.nodes.find(n=>n.id==='explicit-attempt').progress.length,2);
const filtered=layout.reasoningVisible({nodes:[{id:'finished',state:'closed'},{id:'legacy',status:'completed'},{id:'active',status:'active'}],edges:[]},new Set(),'','done');
assert.deepEqual(Array.from(filtered.nodes,n=>n.id),['finished','legacy'],'status filtering supports persisted exploration and legacy task states');

const shared={nodes:[{id:'root',view_kind:'origin'},{id:'a',view_kind:'intent'},{id:'b',view_kind:'intent'},{id:'shared',view_kind:'fact'},{id:'only-a',view_kind:'fact'}],edges:[
  {source:'root',target:'a'},{source:'root',target:'b'},{source:'a',target:'shared'},{source:'b',target:'shared'},{source:'a',target:'only-a'}]};
const folded=layout.reasoningVisible(shared,new Set(['a']));
assert.ok(folded.nodes.some(n=>n.id==='shared'),'a shared fact remains visible through another live parent');
assert.ok(!folded.nodes.some(n=>n.id==='only-a'));
const searched=layout.reasoningVisible({...shared,nodes:shared.nodes.map(n=>({...n,title:n.id}))},new Set(),'shared');
assert.equal(searched.nodes.length,4,'search keeps all explicit ancestors to preserve context');
const cycle={nodes:[{id:'x',view_kind:'intent'},{id:'y',view_kind:'intent'}],edges:[{source:'x',target:'y'},{source:'y',target:'x'}]};
assert.ok(layout.reasoningVisible(cycle,new Set(['x'])).nodes.length>0,'cycles do not hide every expansion control');

const heights=new Map(graph.nodes.map(n=>[n.id,n.id==='hypothesis' ? 180 : 116]));
const placed=layout.reasoningLayout(graph.nodes,graph.edges,heights);
for(const edge of graph.edges)assert.ok(placed.x.get(edge.target)>placed.x.get(edge.source),`${edge.source} -> ${edge.target} must read left-to-right`);
for(let i=0;i<graph.nodes.length;i++)for(let j=i+1;j<graph.nodes.length;j++) {
  const a=graph.nodes[i].id,b=graph.nodes[j].id;
  assert.ok(Math.abs(placed.x.get(a)-placed.x.get(b))>=232 || placed.y.get(a)+heights.get(a)<=placed.y.get(b)
    || placed.y.get(b)+heights.get(b)<=placed.y.get(a),'variable-height cards cannot overlap');
}
const reordered=layout.reasoningLayout([...graph.nodes].reverse(),graph.edges,heights);
for(const node of graph.nodes){assert.equal(reordered.x.get(node.id),placed.x.get(node.id));assert.equal(reordered.y.get(node.id),placed.y.get(node.id));}
assert.equal(layout.reasoningLayout(cycle.nodes,cycle.edges,new Map([['x',116],['y',116]])).x.size,2);
console.log('reasoning topology: separate assets, typed public chain, legacy ownership, shared collapse, stable horizontal layout and cycle handling passed');

// sess-070e5e2f: no intention labels leaves a goal and facts at nonconsecutive depths.
const gapCases=[
  {nodes:[{id:'origin',view_kind:'origin'},{id:'goal',view_kind:'goal'},{id:'fact',view_kind:'fact'}],
   edges:[{source:'origin',target:'goal'},{source:'goal',target:'fact'}]},
  {nodes:[{id:'fact',view_kind:'fact'}],edges:[]},
  {nodes:[{id:'attempt',view_kind:'attempt'},{id:'fact',view_kind:'fact'}],edges:[{source:'attempt',target:'fact'}]},
  {nodes:[],edges:[]},
];
for(const example of gapCases) {
  const result=layout.reasoningLayout(example.nodes,example.edges,new Map(example.nodes.map(n=>[n.id,120])));
  assert.ok(Number.isFinite(result.width) && Number.isFinite(result.height),'missing layers cannot poison canvas dimensions');
  assert.ok([...result.x.values(),...result.y.values()].every(Number.isFinite),'all node coordinates must be finite');
  assert.ok(result.width<=example.nodes.length*332+328,'missing semantic levels must not create empty visual columns');
}
assert.equal(legacy.missing_intents,true,'missing public labels are explained instead of inventing semantic nodes');
assert.equal(hierarchy.missing_intents,false);
console.log('reasoning topology: missing levels and filtered leaf-only graphs retain finite, compact coordinates');
