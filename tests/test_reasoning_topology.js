'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const context=vm.createContext(require('./helpers/i18n').context({}));
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
assert.equal(explicitAndPublic.edges.find(e=>e.source==='explicit-intent' && e.target==='explicit-attempt').kind,'tries');
const persistedRedirect=layout.reasoning({topology:{process:{nodes:[
  {id:'q',kind:'exploration',node_type:'hypothesis',task_id:'t',route_id:'r',title:'读取附件'},
  {id:'old',kind:'exploration',node_type:'attempt',task_id:'t',route_id:'r',title:'直接访问 ZIP'},
  {id:'new',kind:'exploration',node_type:'attempt',task_id:'t',route_id:'r',title:'解压后读取'}
],edges:[{source:'q',target:'old',kind:'derives'},{source:'q',target:'new',kind:'derives'},
  {source:'old',target:'new',kind:'derives'}]},public_stages:{items:[
  {task_id:'t',route_id:'r',turn:1,exploration:{intent:'读取附件',attempt:'直接访问 ZIP'}},
  {task_id:'t',route_id:'r',turn:2,exploration:{intent:'读取附件',attempt:'解压后读取',from:'直接访问 ZIP',reason:'ZIP 不是目录'}}
]}}});
const redirectEdges=persistedRedirect.edges.filter(e=>e.source==='old' && e.target==='new');
assert.equal(redirectEdges.length,1,'同一转向的公开标注和黑板父边合并，不绘制重复连线');
assert.equal(redirectEdges[0].kind,'redirects');
// 轻量公开语义会落到真实黑板：同名方法按意图区分，回放不能覆盖后来的修订。
const lightPersisted=layout.reasoning({topology:{process:{nodes:[
  {id:'light-q1',kind:'exploration',node_type:'hypothesis',task_id:'light',route_id:'r',title:'第一问题'},
  {id:'light-q2',kind:'exploration',node_type:'hypothesis',task_id:'light',route_id:'r',title:'第二问题'},
  {id:'light-a1',kind:'exploration',node_type:'attempt',task_id:'light',route_id:'r',title:'对照测试',state:'refuted',reason:'后续原始证据否定'},
  {id:'light-a2',kind:'exploration',node_type:'attempt',task_id:'light',route_id:'r',title:'对照测试',state:'exhausted',reason:'可用输入已核对'}
],edges:[{source:'light-q1',target:'light-a1',kind:'derives'},{source:'light-q2',target:'light-a2',kind:'derives'}]},public_stages:{items:[
  {task_id:'light',route_id:'r',turn:1,exploration:{intent:'第一问题',attempt:'对照测试',state:'running'}},
  {task_id:'light',route_id:'r',turn:2,exploration:{intent:'第二问题',attempt:'对照测试',state:'running'}}
]}}});
assert.equal(lightPersisted.nodes.filter(n=>n.view_kind==='attempt').length,2,'同名方法分别复用对应意图的黑板节点');
assert.equal(lightPersisted.nodes.find(n=>n.id==='light-a1').progress.length,1);
assert.equal(lightPersisted.nodes.find(n=>n.id==='light-a2').state,'exhausted','旧进展不能重新打开黑板排除的分支');
assert.equal(lightPersisted.nodes.find(n=>n.id==='light-a1').reason,'后续原始证据否定');
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

const significantTurns=Array.from({length:24},(_,i)=>({task_id:'milestone-task',route_id:'main',turn:i+1,
  title:'持续验证',body:'普通执行记录',status:'done',tool_call_ids:[`m-call-${i+1}`]}));
significantTurns[0].exploration={intent:'确认记录隔离',attempt:'授权账号对照',state:'running'};
significantTurns[5].exploration={intent:'确认记录隔离',attempt:'授权账号对照',milestone:{kind:'discovery',title:'发现跨账号返回相同记录',basis:'实际响应中记录标识相同，需确认归属',next:'对照记录所有者',evidence_refs:['topology:record']}};
significantTurns[10].exploration={intent:'确认记录隔离',attempt:'授权账号对照',state:'running'};
significantTurns[17].exploration={intent:'确认记录隔离',attempt:'授权账号对照',milestone:{kind:'breakthrough',title:'归属对照支持存在访问边界问题',basis:'第二个授权账号读到了第一个账号的测试记录',next:'交给独立角色复核'}};
const significantState={tasks:[{task_id:'milestone-task',status:'done'}],topology:{public_stages:{items:significantTurns},process:{nodes:[
 {id:'record',kind:'finding',task_id:'milestone-task',route_id:'main',provenance:{producer:{turn:8}},evidence_state:'unverified'},
 {id:'final',kind:'finding',task_id:'milestone-task',route_id:'main',provenance:{producer:{turn:24}},evidence_state:'unverified'}],edges:[]}}};
const beforeSignificant=JSON.stringify(significantState),significant=layout.reasoning(significantState);
const phases=significant.nodes.filter(n=>n.view_kind==='attempt' || n.node_type==='milestone');
assert.deepEqual(Array.from(phases,n=>n.view_kind),['attempt','discovery','breakthrough']);
assert.deepEqual(Array.from(phases,n=>n.progress.length),[5,12,7],'24 rounds form only three meaningful phases');
assert.equal(phases[1].title,'发现跨账号返回相同记录','same-title updates cannot erase milestone conclusions');
assert.equal(phases[1].next,'对照记录所有者');
for(let i=1;i<phases.length;i++)assert.ok(significant.edges.some(e=>e.source===phases[i-1].id && e.target===phases[i].id && e.kind==='advance'));
assert.ok(significant.edges.some(e=>e.source===phases[1].id && e.target==='record'));
assert.ok(significant.edges.some(e=>e.source===phases[2].id && e.target==='final'));
assert.equal(significant.nodes.find(n=>n.id==='final').evidence_state,'unverified','a breakthrough cannot confirm a vulnerability');
assert.equal(JSON.stringify(significantState),beforeSignificant);
const replaySignificant=layout.reasoning(significantState,[significantTurns[5]]);
assert.deepEqual(Array.from(replaySignificant.nodes,n=>n.id),Array.from(significant.nodes,n=>n.id),'live/replay duplicates do not create additional milestones');
const expanded=layout.reasoning(significantState);
const collapsedMilestone=layout.reasoningVisible(expanded,new Set([phases[1].id]));
assert.ok(!collapsedMilestone.nodes.some(n=>n.id===phases[2].id),'milestone branches support collapse');
const significantLayout=layout.reasoningLayout(significant.nodes,significant.edges,new Map(significant.nodes.map(n=>[n.id,150])));
for(let i=1;i<phases.length;i++)assert.ok(significantLayout.x.get(phases[i].id)>significantLayout.x.get(phases[i-1].id),'milestone phases read left to right');
const parallelSignificant=layout.reasoning({topology:{public_stages:{items:[...significantTurns,
 {task_id:'milestone-task',route_id:'parallel',turn:6,exploration:significantTurns[5].exploration}]}}});
assert.equal(parallelSignificant.nodes.filter(n=>n.view_kind==='discovery').length,2);
const parallelNode=parallelSignificant.nodes.find(n=>n.node_type==='milestone' && n.route_id==='parallel');
assert.ok(!parallelSignificant.edges.some(e=>e.kind==='advance' && (e.source===parallelNode.id || e.target===parallelNode.id)),'separate routes never inherit one another’s progression');
console.log('reasoning milestones: significant boundaries only, ordinary-round absorption, exact output ownership, replay, isolation, collapse and ordered layout passed');
const repeatedState=JSON.parse(beforeSignificant);
repeatedState.topology.public_stages.items[6].exploration=significantTurns[5].exploration;
const repeatedMilestone=layout.reasoning(repeatedState);
assert.equal(repeatedMilestone.nodes.filter(n=>n.node_type==='milestone').length,2,'repeating the identical discovery cannot create another node');
assert.equal(repeatedMilestone.nodes.find(n=>n.view_kind==='discovery').progress.length,12);
const newQuestion=layout.reasoning({topology:{public_stages:{items:[
 {task_id:'t',route_id:'r',turn:1,exploration:{intent:'确认入口',attempt:'读取响应'}},
 {task_id:'t',route_id:'r',turn:2,exploration:{intent:'确认登录条件',attempt:'核对认证',milestone:{kind:'turning_point',title:'入口要求登录',basis:'匿名请求返回认证挑战'}}}
]}}});
const question=newQuestion.nodes.find(n=>n.title==='确认登录条件'),pivot=newQuestion.nodes.find(n=>n.view_kind==='turning_point');
assert.ok(newQuestion.edges.some(e=>e.source===question.id && e.target===pivot.id),'a significant transition retains its new intention as well as public progression');
console.log('reasoning milestones: repeated reports coalesce and changed intentions retain their ownership');
const managedScan=layout.reasoning({session:{title:'主控任务'},tasks:[{task_id:'scan-task',agent_type:'scanner',status:'running'}],topology:{tasks:[
 {task_id:'scan-task',agent_type:'scanner',status:'running',delegation:{goal:'核实已发现服务的模板线索',target:'http://fixture.test',reason:'主控根据用户许可派遣'}}],
 public_stages:{items:[{task_id:'scan-task',route_id:'scan-task',turn:1,title:'选择 nginx 指纹模板',
  tool_call_ids:['scan-call'],tools:[{id:'scan-call',name:'nuclei_scan',status:'done'}],
  exploration:{intent:'核对服务指纹',attempt:'选择本地模板'}}]}}});
const scanAssignment=managedScan.nodes.find(n=>n.id==='__process_scan_assignment:scan-task');
assert.ok(scanAssignment && scanAssignment.actor==='master','real scanner assignment is visible under its dispatcher');
const scanIntent=managedScan.nodes.find(n=>n.view_kind==='intent');
assert.ok(managedScan.edges.some(e=>e.source==='__process_origin' && e.target===scanAssignment.id && e.kind==='spawns'));
assert.ok(managedScan.edges.some(e=>e.source===scanAssignment.id && e.target===scanIntent.id));
assert.equal(managedScan.nodes.find(n=>n.view_kind==='attempt').progress[0].tools[0].name,'nuclei_scan');
assert.ok(!layout.reasoning({scanner:{status:'idle'}}).nodes.some(n=>n.id.startsWith('__process_scan_assignment:')),'idle capacity does not invent a dispatched task');
console.log('managed scanner: actual master assignment, public stages and tool details appear without idle placeholders');
