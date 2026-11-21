// 只由 tsc 检查，不加载到页面；反例未报错时 @ts-expect-error 会使检查失败。
const typedStore = TaskTrace.create();
TaskTrace.reduce(typedStore, {type:'agent.delta', actor:'agent-a', detail:{kind:'text',turn:1,text:'进展'}}, 128);
SessionHistory.enqueue({type:'model.call',detail:{task_id:'t',tokens:10}},128,'session-a');
// 旧事件可以没有 detail/actor/ts；未知事件类型保留兼容。
TaskTrace.reduce(typedStore, {type:'future.event'});

// @ts-expect-error 轮次是数值，不能把旧文本字段误当作轮次。
TaskTrace.push({type:'agent.delta',detail:{turn:'1'}});
// @ts-expect-error SSE 的字节游标进入队列前必须转换为数值。
SessionHistory.enqueue({type:'agent.delta'},'128','session-a');
// @ts-expect-error 事件字段拼写错误不能静默传播。
TaskTrace.push({type:'agent.delta',detail:{taskId:'t'}});
// @ts-expect-error 任务渲染状态使用 done，不能与会话 completed 混用。
typedStore.tasks.get('task').status = 'completed';
// @ts-expect-error 存储的阶段文本必须为字符串。
typedStore.tasks.get('task').texts.set('call',42);
// @ts-expect-error UI 的断线续传游标同样是字节数值。
S.evSeen = '128';
/** @type {import('../../src/spiritscout/web/static/js/frontend-types').AuditPage} */
const pageContract = {items:[],older_cursor:0,cursor:128,has_more:false};
// @ts-expect-error 分页响应必须明确返回是否还有更早历史。
const incompletePage = /** @satisfies {import('../../src/spiritscout/web/static/js/frontend-types').AuditPage} */ ({items:[],older_cursor:0,cursor:128});
// @ts-expect-error API 的历史项必须带独立字节游标，事件 seq 不能替代它。
pageContract.items.push({event:{type:'user.message',seq:1}});
