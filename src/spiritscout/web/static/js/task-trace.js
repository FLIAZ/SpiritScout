"use strict";
/* 公开正文按意图聚合；调用与结果收进对应消息的默认折叠明细。 */
const TaskTrace = (() => {
  const terminal = new Set(["done", "failed", "cancelled", "pruned"]);
  const statuses = {running:I18n.t("执行中"), done:I18n.t("已完成"), failed:I18n.t("执行失败"), cancelled:I18n.t("已撤销"), pruned:I18n.t("已暂缓"), waiting:I18n.t("等待返回")};
  const supported = /^(task\.(start|done|failed|cancelled|pruned)|agent\.(turn|delta|tool_call|tool_result|conclude|note|progress|ask_user|ask_answered|blocked|error)|executor\.identity|model\.(started|call|failed|cancelled))$/;
  const wrapper = name => name === "investigate" || name === "explore";
  const worker = actor => !!actor && !/^(master(?:-|$)|gateway$|runtime$|system$|user$)/.test(actor);
  const create = () => ({tasks:new Map(), routes:new Map(), calls:new Map(), singleActors:new Map()});
  let timelineEpoch = 0, activeIntentKey = null;
  const keyOf = (actor, id) => JSON.stringify([actor, id]);
  const brief = text => String(text || "").replace(/\s+/g, " ").trim().slice(0, 180);
  const textOf = value => typeof value === "string" ? value : value == null ? "" : JSON.stringify(value, null, 2);
  // 子 Agent 印章：名字首字 + 与时间线竖线同源的槽位色（ag-N），同一 Agent 全会话同色
  const agentSlot = actor => {
    if (typeof traceActorSlot === "function") return traceActorSlot(actor);
    const base = String(actor || "?").replace(/-[0-9a-f]{8,}$/i, "");
    let hash = 0;
    for (let i = 0; i < base.length; i++) hash = (hash * 31 + base.charCodeAt(i)) >>> 0;
    return (hash % 8) + 1;
  };
  const sealOf = actor => {
    const name = actorName(actor) || I18n.t("灵");
    return `<i class="task-seal" style="--seal: var(--agent-${agentSlot(actor)})" aria-label="${esc(name)}" title="${esc(name)}">${esc([...name][0])}</i>`;
  };
  function resolve(store, e) {
    const d = e.detail || {}, actor = String(e.actor || "");
    if (d.task_id) return keyOf(actor, d.task_id);
    if (d.call_id && store.calls.has(keyOf(actor,d.call_id))) return store.calls.get(keyOf(actor,d.call_id));
    for (const route of [d.route_id, d.route].filter(Boolean)) {
      const matches = store.routes.get(keyOf(actor,route));
      if (matches?.size === 1) return [...matches][0];
      if (matches?.size > 1) return keyOf(actor, "");
    }
    const candidates = [...store.tasks].filter(([, t]) => t.actor === actor && !terminal.has(t.status));
    return candidates.length === 1 ? candidates[0][0] : keyOf(actor, "");
  }
  function reduce(store, e, eventId = null) {
    if(e.type==='agent.progress') {
      const parsed=parsePublicProgress(e.detail?.text);
      e={...e,detail:{...e.detail,text:parsed.text,...(!e.detail?.exploration && parsed.exploration ? {exploration:parsed.exploration} : {})}};
    }
    const identity = e.detail || {};
    if(!worker(e.actor) && /^task\.(cancelled|failed|pruned)$/.test(e.type) && identity.task_id) {
      // 主控发布的任务生命周期同样作用于实际执行者，不能留下“执行中”的旧卡片。
      let changed=null;
      for(const task of store.tasks.values())if(task.id===identity.task_id)
        changed=reduce(store,{...e,actor:task.actor},eventId) || changed;
      return changed;
    }
    if(e.type === "system.lifecycle" && identity.action === "single_executor_start" &&
        ["ctf","awdp"].includes(identity.mode) && identity.agent_id) {
      store.singleActors.set(identity.agent_id,{hints:[]});
      return null;
    }
    if (!worker(e.actor) || !supported.test(e.type)) return null;
    const d = e.detail || {}, key = resolve(store,e);
    let task = store.tasks.get(key);
    if (!task) {
      task = {key, id:d.task_id || "", actor:e.actor, title:"", status:"running", progress:I18n.t("正在准备"), tools:new Map(),
        texts:new Map(), completedTexts:new Set(), steps:new Map(), segments:new Map(), activeIntents:new Map(), activeSteps:new Map(), routeAliases:new Map(),
        conclusions:new Map(), modelSteps:new Map(), started:false, summary:"", updated:e.ts, event:e, version:0,
        currentSegmentKey:"", segmentSerial:0, startId:eventId, endId:eventId};
      store.tasks.set(key,task);
    }
    if(e.type === "executor.identity" && d.kind === "single") {
      store.singleActors.set(e.actor,{hints:Array.isArray(d.user_direction_hints)
        ? d.user_direction_hints.filter(value=>typeof value==="string") : []});
    }
    if(e.type === "task.start" && d.type === "ctf_single" && !store.singleActors.has(e.actor))
      store.singleActors.set(e.actor,{hints:[]});
    const single = store.singleActors.get(e.actor);
    if(single) {task.singleExecutor=true;task.directionHints=single.hints;}
    task.updated=e.ts; task.version++;
    if(eventId != null) task.endId=eventId;
    if (d.title) task.title=d.title;
    if (!task.title && d.angle) task.title=d.angle;
    for (const route of [d.route_id,d.route].filter(Boolean)) {
      const routeKey=keyOf(e.actor,route);
      if (!store.routes.has(routeKey)) store.routes.set(routeKey,new Set());
      store.routes.get(routeKey).add(key);
    }
    if(d.call_id) store.calls.set(keyOf(e.actor,d.call_id),key);
    if(d.route_id && d.route) {
      if(!task.routeAliases.has(d.route)) task.routeAliases.set(d.route,new Set());
      task.routeAliases.get(d.route).add(d.route_id);
    }
    const aliases=task.routeAliases.get(d.route);
    let route=d.route_id || (aliases?.size === 1 ? [...aliases][0] : d.route) || "";
    let step=route ? task.activeSteps.get(route) : null;
    const ensureStep = (stepKey, turn=0) => {
      if(!task.steps.has(stepKey)) {
        task.steps.set(stepKey,{key:stepKey,route,turn,event:e,texts:new Map(),status:terminal.has(task.status) ? task.status : "running",progress:I18n.t("正在分析")});
      }
      const next=task.steps.get(stepKey), previous=task.activeSteps.get(route);
      // 迟到事件只更新原阶段；不会把当前阶段回退，也不提前宣告在途工具完成。
      if(!previous || !turn || turn>=previous.turn) {
        if(previous && previous!==next && !terminal.has(previous.status)) {
          previous.status=[...task.tools.values()].some(t=>t.stepKey===previous.key && t.status==="running") ? "waiting" : "done";
        }
        task.activeSteps.set(route,next);
      }
      return next;
    };
    if(route && Number.isInteger(d.turn) && d.turn>0) step=ensureStep(JSON.stringify([route,d.turn]),d.turn);
    else if(d.call_id && task.modelSteps.has(d.call_id)) step=task.steps.get(task.modelSteps.get(d.call_id));
    // 没有回合号的旧记录与 SRC 外层动作，按实际调用建立阶段，不凭空生成计划步骤。
    if(e.type === "agent.tool_call" && (d.calls || []).some(c=>!wrapper(c.name)) && (!step || !step.turn && terminal.has(step.status))) {
      step=ensureStep(JSON.stringify([route,"tool",d.calls[0]?.id || e.seq || e.ts]));
    }
    if(e.type === "model.started" && route && (!step || !step.turn && terminal.has(step.status))) {
      step=ensureStep(JSON.stringify([route,"model",d.call_id || e.seq || e.ts]));
    }
    if(d.call_id && step) task.modelSteps.set(d.call_id,step.key);
    if(step && !route) route=step.route;
    let linkedTool=null;
    if(e.type === "agent.tool_result") {
      linkedTool=task.tools.get(JSON.stringify([route,d.id]));
      if(!linkedTool && d.id) {
        const matches=[...task.tools.values()].filter(tool=>tool.id===d.id);
        if(matches.length===1) linkedTool=matches[0];
      }
      const owner=task.steps.get(linkedTool?.stepKey);
      if(owner) {step=owner;route=owner.route;}
    }
    const stem=JSON.stringify([route,d.call_id || d.turn || ""]);
    if(e.type === "agent.progress" || /^model\.(call|failed|cancelled)$/.test(e.type)) task.completedTexts.add(stem);
    if(step?.turn && (e.type === "agent.tool_call" || /^model\.(call|failed|cancelled)$/.test(e.type)))
      task.completedTexts.add(JSON.stringify([route,step.turn]));
    const previousDeltaText=e.type === "agent.delta" ? task.texts.get(stem) || "" : "";
    const nextDeltaText=previousDeltaText+(d.text || "");
    const structuredDelta=/^\s*[\[{]|^\s*```/m.test(nextDeltaText);
    const deltaPublicText=structuredDelta ? commentaryText(nextDeltaText) : commentaryText(d.text);
    const angle=e.type === "agent.turn" && typeof d.angle === "string" ? d.angle.trim() : "";
    const publicText=e.type === "agent.delta" && !["reasoning","tool_call","reset"].includes(d.kind)
      ? deltaPublicText
      : e.type === "agent.progress" ? commentaryText(d.text)
      : e.type === "agent.note" ? commentaryText(d.text)
      : e.type === "agent.conclude" ? commentaryText(d.summary)
      : ["agent.ask_user","agent.blocked"].includes(e.type) ? textOf(d.question || d.reason || d.message) : "";
    const semantic=e.type === "agent.progress" && typeof d.exploration?.intent === "string"
      ? d.exploration.intent.trim() : "";
    const publicMessage=!!(publicText || angle || semantic);
    const segmentable=!/^task\./.test(e.type) && e.type!=="model.call" &&
      !(e.type==="agent.delta" && ["reasoning","tool_call"].includes(d.kind));
    let segment=null;
    if(segmentable) {
      const linked=linkedTool?.intentKey || step?.intentKey;
      const owner=task.segments.get(linked) || task.activeIntents.get(route);
      const intent=semantic || angle || owner?.intent || (publicText ? brief(publicText.split(/\n|[。！？]/)[0]) : "");
      if(publicMessage) {
        const adopt=owner && !owner.intent;
        const same=owner && owner.key===activeIntentKey && owner.epoch===timelineEpoch &&
          ((!semantic && !angle) || owner.intent===intent || !owner.texts.size);
        const streamedOwner=e.type==='agent.progress' && d.streamed && owner && step?.intentKey===owner.key
          && (!owner.declaredIntent || !semantic || owner.declaredIntent===semantic);
        if(adopt || same || streamedOwner) {
          segment=owner;
          if(semantic || !segment.intent) segment.intent=intent;
          if(adopt) {segment.event=e;if(eventId != null) segment.startId=eventId;}
          segment.epoch=timelineEpoch;
        } else {
          const marker=eventId ?? e.seq ?? e.ts ?? `${e.type}:${++task.segmentSerial}`;
          const segmentKey=JSON.stringify([task.key,route,intent,String(marker)]);
          segment={key:segmentKey,stageKey:intent,stepKey:step?.key || "",route,turn:step?.turn || 0,
            intent,epoch:timelineEpoch,event:e,startId:eventId,endId:eventId,texts:new Map(),toolKeys:new Set(),records:new Map(),
            status:"running",progress:"",title:angle,version:0};
          task.segments.set(segmentKey,segment);
          if(semantic && owner && d.streamed && step) {
            // 元数据在正文流结束后才到：仅迁移本回合，保留原意图已有的正文和调用。
            for(const [key,value] of step.texts)if(owner.texts.has(key)) {
              segment.texts.set(key,value);owner.texts.delete(key);
            }
            for(const [key,tool] of task.tools)if(tool.stepKey===step.key && owner.toolKeys.has(key)) {
              owner.toolKeys.delete(key);segment.toolKeys.add(key);tool.intentKey=segment.key;
            }
          }
        }
        if(semantic)segment.declaredIntent=semantic;
        task.activeIntents.set(route,segment);
        task.currentSegmentKey=segment.key;
        activeIntentKey=segment.key;
        if(step) step.intentKey=segment.key;
      } else {
        segment=owner;
        if(!segment) {
          const segmentKey=JSON.stringify([task.key,route,"pending-execution"]);
          segment={key:segmentKey,stageKey:route,stepKey:step?.key || "",route,turn:0,intent:"",
            epoch:timelineEpoch,event:e,startId:eventId,endId:eventId,texts:new Map(),toolKeys:new Set(),records:new Map(),
            status:"running",progress:"",title:"",version:0};
          task.segments.set(segmentKey,segment);task.activeIntents.set(route,segment);
        }
        if(step && !step.intentKey) step.intentKey=segment.key;
      }
      segment.version++;
      if(eventId != null) segment.endId=eventId;
      // 明细只补充实际错误；模型轮次与开始/取消事件仍参与状态更新，不堆成调用流水。
      if(["model.failed","agent.error"].includes(e.type)) {
        const recordKey=JSON.stringify([e.type,step?.key || route,d.call_id || ""]);
        segment.records.set(recordKey,textOf(d.error || I18n.t("调用失败")));
      }
    }
    if (e.type === "task.start") { task.started=true;task.status="running";task.progress=I18n.t("开始执行");task.target=d.target || ""; }
    if (e.type === "agent.turn") {
      task.progress=I18n.t("正在推进");
      if(step) {step.title=d.angle || step.title;step.progress=I18n.t("正在分析");}
    }
    if (e.type === "model.started") {task.progress=I18n.t("正在分析下一步");if(step) {step.progress=I18n.t("正在分析");if(!terminal.has(task.status)) step.status="running";}}
    if (e.type === "agent.delta") {
      if (d.kind === "reset") {
        task.texts.delete(stem);task.completedTexts.delete(stem);step?.texts.delete(stem);
        // 同一输出可能跨过用户消息；重试只撤回该输出，不留下另一张卡里的旧碎片。
        for(const item of task.segments.values()) item.texts.delete(stem);
      }
      else if (d.kind !== "reasoning" && d.kind !== "tool_call" && d.text) {
        const text=nextDeltaText;
        task.texts.set(stem,text.slice(-24000)); task.progress=I18n.t("正在更新进展");
        if(step) {step.texts.set(stem,text.slice(-24000));step.progress=I18n.t("正在更新进展");}
        if(segment) {
          let segmentText=(segment.texts.get(stem) || "")+d.text;
          // JSON 的公开说明只能在完整解析后提取，不能从跨段的半截协议中提取。
          if(structuredDelta && publicText && !commentaryText(segmentText)) segmentText=text;
          segment.texts.set(stem,segmentText.slice(-24000));
        }
      }
    }
    if (e.type === "agent.progress") {
      if(step && d.exploration)step.exploration=d.exploration;
      const text=commentaryText(d.text);
      if(d.streamed && text && step?.texts.has(stem) && commentaryText(step.texts.get(stem))!==text) {
        // 历史分页可能从 JSON 中段开始；完整持久正文到达后撤掉残片，不能并排显示。
        task.texts.set(stem,text);step.texts.set(stem,text);
        for(const item of task.segments.values())item.texts.delete(stem);
        segment?.texts.set(stem,text);
      }
      const alreadyStreamed=d.streamed && step && [...step.texts.values()].some(raw=>commentaryText(raw)===text);
      if(text && !alreadyStreamed) {
        const publicKey=`progress:${d.call_id || e.seq || e.ts || task.version}`;
        task.texts.set(publicKey,text);
        if(step) step.texts.set(publicKey,text);
        if(segment) segment.texts.set(publicKey,text);
      }
    }
    if (e.type === "agent.ask_user" || e.type === "agent.blocked") {
      task.waitingReasons ||= new Map();
      task.waitingReasons.set(route,textOf(d.question || d.reason || d.message || I18n.t("等待补齐必要条件")));
      task.waitingReason=[...task.waitingReasons.values()].join("；");
      task.progress=task.waitingReason;
      if(segment) segment.texts.set(`waiting:${e.seq || e.ts}`,task.waitingReason);
      if(step && !terminal.has(task.status)) {step.status="waiting";step.progress=task.waitingReason;}
    }
    if (e.type === "agent.ask_answered") {
      task.waitingReasons?.delete(route);
      task.waitingReason=[...(task.waitingReasons?.values() || [])].join("；");
      task.progress=I18n.t("已收到答复，正在继续验证");
      if(step && !terminal.has(task.status)) {step.status="running";step.progress=task.progress;}
    }
    if (e.type === "agent.note") {
      const noteKey=`note:${e.seq || e.ts}`, note=textOf(d.text);
      task.progress=I18n.t("已更新进展");task.texts.set(noteKey,note);
      if(step) step.texts.set(noteKey,note);
      if(segment) segment.texts.set(noteKey,note);
    }
    if (e.type === "agent.tool_call") {
      for (const c of d.calls || []) {
        const toolKey=JSON.stringify([route,c.id || `${e.seq || e.ts}:${task.tools.size}`]);
        task.tools.set(toolKey,{key:toolKey,call:c,route,id:c.id,name:c.name,status:"running",result:"",note:"",version:1,
          stepKey:wrapper(c.name) ? "" : step?.key || "", intentKey:segment?.key || ""});
        segment?.toolKeys.add(toolKey);
      }
      if(step) {step.status=terminal.has(task.status) ? task.status : "running";step.progress=I18n.t("工具执行中");}
      task.progress=(d.calls || []).length ? I18n.t`正在调用 ${d.calls.map(c => SessionObserver.toolName(c.name)).join("、")}` : task.progress;
    }
    if (e.type === "agent.tool_result") {
      let tool=linkedTool;
      if (!tool && d.id) {
        const matches=[...task.tools.values()].filter(t => t.id === d.id);
        if(matches.length === 1) tool=matches[0];
      }
      if (!tool) {
        const toolKey=JSON.stringify([route,d.id || `result:${e.seq || e.ts}`]);
        tool={key:toolKey,call:{name:d.name},route,id:d.id,name:d.name || I18n.t("工具"),version:0,missingCall:true,
          stepKey:wrapper(d.name) ? "" : step?.key || ""};
        task.tools.set(toolKey,tool);
      }
      tool.status=d.ok === false ? "failed" : "done";tool.version++;
      const raw=SessionObserver.cleanText(textOf(d.result ?? d.note ?? d.error ?? I18n.t("未记录原始响应")));
      tool.result=raw.slice(0,24000);tool.clipped=raw.length>24000;
      tool.note=SessionObserver.resultSummary(d);
      if(Array.isArray(d.images))tool.images=d.images;
      if(d.ok !== false && d.skill?.skill_id)tool.skill=d.skill;
      segment?.toolKeys.add(tool.key);
      task.progress=`${SessionObserver.toolName(tool.name)} · ${d.ok === false ? I18n.t("失败") : I18n.t("已返回")}${tool.note ? `：${brief(tool.note)}` : ""}`;
      const owner=task.steps.get(tool.stepKey);
      if(owner) {
        const pending=[...task.tools.values()].some(t=>t.stepKey===owner.key && t.status==="running");
        if(!terminal.has(task.status)) {
          owner.status=pending ? (task.activeSteps.get(owner.route)===owner ? "running" : "waiting") :
            [...task.tools.values()].some(t=>t.stepKey===owner.key && t.status==="failed") ? "failed" : "done";
          owner.progress=pending ? I18n.t("等待其他工具返回") : I18n.t("本次执行已返回");
        }
      }
    }
    if (e.type === "agent.conclude") {
      if (d.summary) {
        const conclusion=textOf(d.summary);
        task.conclusions.set(route,conclusion);
        segment?.texts.set(`conclusion:${e.seq || e.ts}`,conclusion);
      }
      task.progress=I18n.t("已整理路线结论");
      if(step) {if(!terminal.has(task.status)) step.status="done";step.progress=I18n.t("已整理结论");}
    }
    if (e.type === "agent.error" || e.type === "model.failed") task.progress=brief(d.error) || I18n.t("执行遇到问题，等待下一步");
    if (e.type === "model.cancelled") task.progress=I18n.t("本次模型调用已取消");
    if(step && ["agent.error","model.failed","model.cancelled"].includes(e.type)) {
      step.status=e.type==="model.cancelled" ? "cancelled" : "failed";
      step.progress=brief(d.error) || task.progress;
    }
    if (/^task\.(done|failed|cancelled|pruned)$/.test(e.type)) {
      task.endEvent=e;task.status=e.type.slice(5);task.waitingReason="";task.waitingReasons?.clear();
      for(const s of task.steps.values()) if(!terminal.has(s.status)) s.status=task.status;
      for(const item of task.segments.values()) if(!terminal.has(item.status)) item.status=task.status;
      if(d.conclusions?.length) task.conclusions.clear();
      for (const item of d.conclusions || []) {
        if(item.text) task.conclusions.set(item.route || "",textOf(item.text));
      }
      const summary=d.summary;
      if (typeof summary === "string") task.summary=summary;
      else if (summary && typeof summary === "object") {
        task.summary=textOf(summary.conclusion || summary.summary || "");
        task.stats=summary.routes_total != null ? I18n.t`路线完成 ${summary.routes_done || 0}/${summary.routes_total}` : "";
      }
      if (d.error) task.summary=textOf(d.error);
      task.progress=statuses[task.status];
    }
    if(segment) {
      segment.status=task.waitingReason && !terminal.has(task.status) ? "waiting" : task.status;
      segment.progress=step?.progress || task.progress;
      segment.title=segment.title || step?.title || d.angle || "";
      segment.route=step?.route || segment.route;
      segment.turn=step?.turn || segment.turn;
    }
    return task;
  }
  // 正文与展示标注分开解析；括号/引号按 JSON 语法配对，不靠正则截取对象。
  function parsePublicProgress(raw) {
    const source=String(raw || "").trim(),prose=[],bodies=[];
    let cursor=0,exploration=null;
    const protocol=new Set(['progress','commentary','exploration','discovery','analysis','actions','steps',
      'tool_calls','skill_requests','decision','verdict','dag_nodes','local_tool','web_search']);
    function decode(start) {
      while(/\s/.test(source[start] || '') && start<source.length)start++;
      const stack=[];let quoted=false,escaped=false;
      for(let i=start;i<source.length;i++) {
        const c=source[i];
        if(quoted) {if(escaped)escaped=false;else if(c.charCodeAt(0)===92)escaped=true;else if(c==='"')quoted=false;continue;}
        if(c==='"')quoted=true;
        else if(c==='{' || c==='[')stack.push(c);
        else if(c==='}' || c===']') {
          if(!stack.length)return null;
          stack.pop();
          if(!stack.length) {try{return {data:JSON.parse(source.slice(start,i+1)),end:i+1};}catch{return null;}}
        }
      }
      return null;
    }
    function semantic(value,milestone) {
      if(!value || Array.isArray(value) || typeof value!=='object' ||
          ['intent','attempt'].some(k=>typeof value[k]!=='string' || !value[k].trim() || value[k].length>100))return null;
      const result={intent:value.intent.trim(),attempt:value.attempt.trim()};
      for(const [key,limit] of [['from',100],['reason',400]])if(typeof value[key]==='string' && value[key].trim())result[key]=value[key].trim().slice(0,limit);
      if(['running','blocked','supported','refuted','exhausted','closed'].includes(value.state))result.state=value.state;
      const m=value.milestone ?? milestone;
      if(m && ['discovery','breakthrough','turning_point'].includes(m.kind) &&
          ['title','basis'].every(k=>typeof m[k]==='string' && m[k].trim())) {
        result.milestone={kind:m.kind,title:m.title.trim().slice(0,100),basis:m.basis.trim().slice(0,600)};
        if(typeof m.next==='string')result.milestone.next=m.next.trim().slice(0,400);
        if(Array.isArray(m.evidence_refs))result.milestone.evidence_refs=[...new Set(m.evidence_refs.slice(0,20).filter(r=>typeof r==='string' && r.trim()).map(r=>r.trim().slice(0,300)))];
      }
      return result;
    }
    const markers=/^[ \t]*(?:```[^\n]*|[\{\[])/gm;
    for(const marker of source.matchAll(markers)) {
      const start=marker.index;if(start<cursor)continue;
      const token=marker[0].trimStart(),fenced=token.startsWith('```');
      let end=source.length,valueStart=start+marker[0].length-1;
      if(fenced) {
        const lineEnd=source.indexOf('\n',start),close=lineEnd>=0 ? /^[ \t]*```[ \t]*(?:\n|$)/m.exec(source.slice(lineEnd+1)) : null;
        end=close ? lineEnd+1+close.index+close[0].length : source.length;
        if(!['','json'].includes(token.slice(3).trim().toLowerCase())) {prose.push(source.slice(cursor,end));cursor=end;continue;}
        valueStart=lineEnd>=0 ? lineEnd+1 : source.length;
      }
      const parsed=decode(valueStart);
      if(!parsed) {prose.push(source.slice(cursor,start));cursor=end;continue;}
      if(!fenced)end=parsed.end;
      const data=parsed.data,isObject=data && !Array.isArray(data) && typeof data==='object';
      const isProtocol=isObject && Object.keys(data).some(k=>protocol.has(k));
      if(!isProtocol && (prose.length || source.slice(0,start).trim())) {prose.push(source.slice(cursor,end));cursor=end;continue;}
      prose.push(source.slice(cursor,start));cursor=end;
      if(!isObject)continue;
      for(const key of ['progress','commentary'])if(typeof data[key]==='string' && data[key].trim())bodies.push(data[key].trim());
      exploration=semantic(data.exploration,data.milestone) || exploration;
    }
    prose.push(source.slice(cursor));
    const text=prose.join('').trim() || [...new Set(bodies)].join('\n\n');
    return {text,...(exploration ? {exploration} : {})};
  }
  const commentaryText=raw=>parsePublicProgress(raw).text;
  function updatesOf(task) {
    const seen=new Set(), updates=[];
    for(const [key,raw] of task.texts) {
      const text=commentaryText(raw);
      if(!text || text === task.title || seen.has(text)) continue;
      seen.add(text);updates.push({key,text});
    }
    return updates;
  }
  function summaryOf(task) {
    const label=text=>String(text || "").replace(/[#*_`]/g, "").replace(/\s+/g, "").trim();
    const hasBody=text=>label(text) && label(text)!==label(task.title);
    const conclusions=[...new Set(task.conclusions.values())].filter(hasBody);
    if(hasBody(task.summary) || conclusions.length) return hasBody(task.summary) ? task.summary : conclusions.join("\n\n");
    const latest=updatesOf(task).at(-1)?.text;
    if(latest) return I18n.t`${statuses[task.status]}${task.stats ? `，${task.stats}` : ""}。\n\n最后一次进展：\n${latest}`;
    const tools=[...task.tools.values()];
    const results=tools.filter(t=>t.status === "done").length, failures=tools.filter(t=>t.status === "failed").length;
    return `${statuses[task.status]}${task.stats ? `，${task.stats}` : ""}。`+
      (tools.length ? I18n.t`本页记录了 ${tools.length} 次工具调用，${results} 次返回、${failures} 次失败。` : "")+
      I18n.t("当前记录未包含文字结论。");
  }
  function stageSummary(task, step) {
    const notes=[...new Set([...step.texts.values()].map(commentaryText).filter(Boolean))];
    if(!notes.length && terminal.has(step.status)) return I18n.t`本阶段${statuses[step.status]}，记录中未包含文字总结，可展开执行明细。`;
    return notes.at(-1) || (step.turn ? I18n.t("本阶段尚未收到文字进展，正在执行验证。") : I18n.t("尚未形成阶段结论，执行明细可展开查看。"));
  }
  function liveSummary(task) {
    const text=updatesOf(task).at(-1)?.text;
    if(terminal.has(task.status)) return summaryOf(task);
    return text || (task.title ? I18n.t`正在处理：${task.title}。尚未收到阶段结论。` : I18n.t("正在准备任务，尚未收到阶段进展。"));
  }
  let store=create();
  function skillsOf(task) {
    const skills=new Map();
    for(const tool of task.tools.values()) {
      if(tool.status !== "done" || !tool.skill?.skill_id)continue;
      const skill=tool.skill,key=JSON.stringify([skill.skill_id,skill.version,skill.revision]);
      if(!skills.has(key))skills.set(key,{id:skill.skill_id,version:skill.version,paths:new Set()});
      if(skill.path)skills.get(key).paths.add(skill.path);
    }
    return [...skills.values()].map(skill=>({...skill,paths:[...skill.paths]}));
  }
  const rows=new Map(), segmentRows=new Map(), summaryRows=new Map(), dirty=new Set();
  let timer=null;
  const hasNarrative=segment=>!!segment.intent || [...segment.texts.values()].some(raw=>!!commentaryText(raw));
  function segmentTitle(task,segment) {
    if(segment.intent) return segment.intent;
    const text=[...segment.texts.values()].map(commentaryText).find(Boolean);
    return text ? brief(text.split(/\n|[。！？]/)[0]).slice(0,100) : task.title;
  }
  const segmentNotes=segment=>[...new Set([...segment.texts.values()].map(commentaryText).filter(Boolean))];
  function segmentSummary(segment) { return segmentNotes(segment).join("\n\n"); }
  function narrativeOf(task, segment) {
    const notes=[], seen=new Set();
    let repeated=0;
    for(const [key,raw] of segment.texts) {
      const text=commentaryText(raw);
      if(!text) continue;
      const complete=!key.startsWith("[") || task.completedTexts.has(key) || terminal.has(task.status);
      if(seen.has(text)) {if(complete) repeated++;continue;}
      // 完整旧段落已在屏幕上时，重复流的前缀暂不另起一段。
      // 一旦内容不同或以较短的新句结束，仍按原样展示，不能吞掉有效更新。
      if(!complete && notes.some(note=>note.complete && note.text.startsWith(text))) continue;
      seen.add(text);notes.push({key,text,complete});
    }
    return {notes,repeated};
  }
  function renderNarrative(target, notes) {
    const keys=new Set(notes.map(note=>note.key));
    for(const child of [...target.children]) if(!keys.has(child.dataset.narrativeKey)) child.remove();
    notes.forEach((note,index)=>{
      let child=[...target.children].find(el=>el.dataset.narrativeKey===note.key);
      if(!child) {
        target.insertAdjacentHTML("beforeend",'<div class="task-narrative-turn"></div>');
        child=target.lastElementChild;child.dataset.narrativeKey=note.key;
      }
      if(target.children[index]!==child) target.insertBefore(child,target.children[index] || null);
      if(child.dataset.text!==note.text) {
        child.dataset.text=note.text;child.innerHTML=mdRender(note.text);
      }
    });
  }
  function outcomeText(task) {
    const keyOfText=value=>String(value || "").trim();
    const shown=new Set();
    for(const segment of task.segments.values()) {
      for(const raw of segment.texts.values()) {
        const text=keyOfText(commentaryText(raw));
        if(text) shown.add(text);
      }
      const summary=keyOfText(segmentSummary(segment));
      if(summary) shown.add(summary);
    }
    const seen=new Set(), pending=[];
    for(const raw of [task.summary,...task.conclusions.values()]) {
      const text=keyOfText(raw);
      if(!text || text===keyOfText(task.title) || seen.has(text) || shown.has(text)) continue;
      seen.add(text);pending.push(text);
    }
    return pending.join("\n\n");
  }
  function renderRecords(records,list) {
    const text=[...records.values()].join("\n");
    if(list.dataset.text!==text) {list.dataset.text=text;list.textContent=text;}
    list.hidden=!text;
  }
  function stepTitle(task,step) {
    const text=[...step.texts.values()].map(commentaryText).find(Boolean);
    if(text) return brief(text.split(/\n|[。！？]/)[0]).slice(0,100);
    if(step.title && step.title!==task.title) return brief(step.title);
    const names=[...new Set([...task.tools.values()].filter(t=>t.stepKey===step.key).map(t=>SessionObserver.toolName(t.name)))];
    return names.join("、") || I18n.t("分析当前现场");
  }
  // 意图标签：优先事件里带出的路线名，其次该路线首个阶段的切入角度
  function routeLabel(task,route) {
    for(const [name,ids] of task.routeAliases) if(ids.has(route)) return name;
    const titled=[...task.steps.values()].find(s=>s.route===route && s.title && s.title!==task.title);
    if(titled) return brief(titled.title).slice(0,40);
    return route ? I18n.t`路线 ${String(route).slice(-6)}` : I18n.t("默认路线");
  }
  function renderTools(task,tools,list) {
    for(const tool of tools) {
      let el=[...list.children].find(child=>child.dataset.toolKey === tool.key);
      if(!el) {
        list.insertAdjacentHTML("beforeend",`<div class="tl-act"><button class="hd" type="button" aria-expanded="false"><span class="nm"></span><span class="ar"></span><span class="st"></span><span class="cv" aria-hidden="true">⌄</span></button><div class="tl-images"></div><div class="nt"></div><pre class="tl-output"></pre></div>`);
        el=list.lastElementChild;el.dataset.toolKey=tool.key;
        const button=el.querySelector("button.hd");
        button.onclick=()=>{
          const open=el.classList.toggle("open");button.setAttribute("aria-expanded",String(open));
          const parameters=el.querySelector(".tl-parameters");if(parameters) parameters.open=open;
        };
        if(!tool.missingCall) ToolParameters.attach(el,tool.call);
      }
      if(el.dataset.version === String(tool.version)) continue;
      el.dataset.version=String(tool.version);
      el.querySelector(".nm").textContent=SessionObserver.toolName(tool.name);
      el.querySelector(".ar").textContent=tool.missingCall ? I18n.t("调用参数见更早记录") : tool.call.preview || argBrief(tool.name || "",tool.call.arguments);
      el.querySelector(".st").textContent=tool.status === "running" ? I18n.t("进行中") : tool.status === "failed" ? I18n.t("失败") : I18n.t("已返回");
      el.querySelector(".st").className=`st ${tool.status === "failed" ? "n" : tool.status === "done" ? "y" : ""}`;
      el.querySelector(".nt").textContent=tool.note || "";
      const images=el.querySelector(".tl-images");
      images.innerHTML=typeof FileCards!=="undefined" ? (tool.images || []).map(image=>FileCards.imageCard(image)).join("") : "";
      images.hidden=!images.innerHTML;
      el.querySelector(".tl-output").textContent=(tool.result || I18n.t("等待工具返回"))+(tool.clipped ? I18n.t("\n\n此处展示前 24,000 字，完整内容可导出审计查看。") : "");
    }
  }
  function executorLabel(task) {return task.singleExecutor ? I18n.t("灵枢") : actorName(task.actor);}
  function directionLabel(task) {return task.directionHints?.length ? I18n.t`用户方向：${task.directionHints.join("、")}（提示）` : "";}
  function actorLabel(actor) {return store.singleActors.has(actor) ? I18n.t("灵枢") : "";}
  function render(task) {
    const narratives=[...task.segments.values()].filter(hasNarrative);
    const pending=[...task.segments.values()].filter(segment=>!hasNarrative(segment));
    const pendingTools=[...new Set(pending.flatMap(segment=>[...segment.toolKeys]))]
      .map(key=>task.tools.get(key)).filter(tool=>tool && !wrapper(tool.name));
    let row=rows.get(task.key);
    if(row || task.started || task.title || pendingTools.length) {
      if(!row?.isConnected) {
        row=traceAppend(task.event,I18n.html`<details class="task-thread task-dispatch task-execution-only" aria-label="任务执行记录">
          <summary class="task-heading">${sealOf(task.actor)}<strong></strong><span class="task-status"></span></summary>
          <p class="task-context"></p><p class="task-skills" hidden></p>
          <div class="task-tools"></div><pre class="task-execution-records"></pre>
        </details>`,"tl-task");
        rows.set(task.key,row);row.dataset.taskId=task.id;row.dataset.taskPart="dispatch";
      }
      if(task.startId != null) row.dataset.historyId=row.dataset.historyStart=String(task.startId);
      if(task.endId != null) row.dataset.historyEnd=String(task.endId);
      row.hidden=!!narratives.length && !pendingTools.length;
      const root=row.querySelector(".task-thread");
      root.querySelector(".task-heading strong").textContent=task.title || I18n.t("此前意图的执行明细");
      root.dataset.status=task.status;row.dataset.taskStatus=task.status;
      root.querySelector(".task-status").textContent=statuses[task.status] || I18n.t("执行中");
      root.querySelector(".task-context").textContent=[executorLabel(task),directionLabel(task),task.target || task.id].filter(Boolean).join(" · ");
      const skillLine=root.querySelector(".task-skills"),skills=skillsOf(task);
      skillLine.hidden=!skills.length;skillLine.textContent=skills.map(skill=>I18n.t`使用技能「${skill.id}」· v${skill.version} · 已读 ${skill.paths.length} 个文件`).join("；");
      renderTools(task,pendingTools,root.querySelector(".task-tools"));
      renderRecords(new Map(pending.flatMap(segment=>[...segment.records])),root.querySelector(".task-execution-records"));
    }
    for(const segment of narratives) {
      let segmentRow=segmentRows.get(segment.key);
      if(!segmentRow?.isConnected) {
        segmentRow=traceAppend(segment.event,I18n.html`<section class="task-thread task-stage task-continuation" aria-label="意图消息">
          <p class="task-context"></p>
          <header class="task-heading">${sealOf(task.actor)}<span class="task-marker" aria-hidden="true"></span><strong></strong><span class="task-status"></span></header>
          <div class="task-stage-summary md" aria-live="polite"></div>
          <p class="task-repetition" role="status" hidden></p>
          <details class="task-step"><summary>执行明细 <span class="task-tool-count"></span></summary>
            <p class="task-progress"></p><div class="task-tools"></div><pre class="task-execution-records"></pre>
          </details></section>`,"tl-task tl-task-step");
        segmentRows.set(segment.key,segmentRow);
        segmentRow.dataset.taskId=task.id;segmentRow.dataset.stepKey=segment.key;segmentRow.dataset.taskPart="continuation";
      }
      if(segment.startId != null) segmentRow.dataset.historyId=segmentRow.dataset.historyStart=String(segment.startId);
      if(segment.endId != null) segmentRow.dataset.historyEnd=String(segment.endId);
      segmentRow.dataset.taskStatus=segment.status;
      const stage=segmentRow.querySelector(".task-stage"), group=stage.querySelector(".task-step");
      stage.dataset.status=segment.status;group.dataset.stepKey=segment.key;group.dataset.status=segment.status;
      stage.querySelector(".task-context").textContent=[executorLabel(task),directionLabel(task),routeLabel(task,segment.route)].filter(Boolean).join(" · ");
      stage.querySelector(".task-heading strong").textContent=segmentTitle(task,segment);
      stage.querySelector(".task-status").textContent=statuses[segment.status] || I18n.t("执行中");
      group.querySelector(".task-progress").textContent=segment.progress || "";
      const {notes,repeated}=narrativeOf(task,segment), text=notes.map(note=>note.text).join("\n\n");
      const target=stage.querySelector(".task-stage-summary");
      renderNarrative(target,notes);
      const repetition=stage.querySelector(".task-repetition");
      repetition.hidden=!repeated;
      const repeatText=repeated ? I18n.t`相同说明重复出现 ${repeated} 次，已合并展示。` : "";
      if(repetition.textContent!==repeatText) repetition.textContent=repeatText;
      const normalize=value=>String(value || "").trim().replace(/[。.!！]+$/u, "");
      target.hidden=!text || normalize(text)===normalize(segmentTitle(task,segment));
      const owned=[...segment.toolKeys].map(key=>task.tools.get(key)).filter(tool=>tool && !wrapper(tool.name)), list=group.querySelector(".task-tools");
      list.hidden=!owned.length;
      group.hidden=!owned.length && !segment.records.size;
      group.querySelector(".task-tool-count").textContent=owned.length
        ? I18n.t`· ${owned.filter(tool=>tool.status!=="running").length}/${owned.length} 次返回` : I18n.t`· ${segment.records.size} 条错误信息`;
      renderTools(task,owned,list);
      renderRecords(segment.records,group.querySelector(".task-execution-records"));
    }
    if(terminal.has(task.status) && outcomeText(task)) {
      let summary=summaryRows.get(task.key);
      if(!summary?.isConnected) {
        summary=traceAppend(task.endEvent || task.event,I18n.html`<section class="task-thread task-outcome" aria-label="子任务结论"><header class="task-heading">${sealOf(task.actor)}<span class="task-marker" aria-hidden="true"></span><strong></strong><span class="task-status"></span></header><div class="task-summary md"></div></section>`,"tl-task");
        summaryRows.set(task.key,summary);summary.dataset.taskId=task.id;summary.dataset.taskPart="summary";
      }
      if(task.endId != null) summary.dataset.historyId=summary.dataset.historyStart=String(task.endId);
      const outcome=summary.querySelector(".task-thread");outcome.dataset.status=task.status;
      summary.dataset.taskStatus=task.status;
      outcome.querySelector("strong").textContent=I18n.t`${task.title || task.id || actorName(task.actor)} · 子任务结论`;
      outcome.querySelector(".task-status").textContent=statuses[task.status];
      outcome.querySelector(".task-summary").innerHTML=mdRender(outcomeText(task));
    }
    traceScroll();
    if (S.tab === "topo" && typeof renderTopoActivity === "function") renderTopoActivity();
  }
  function push(e, eventId = null) {
    if(["user.message","user.attachment","master.reply","master.reply_failed","agent.reply","agent.reply_failed"].includes(e.type)) {
      timelineEpoch++;activeIntentKey=null;return false;
    }
    const task=reduce(store,e,eventId);
    if(!task) return false;
    if(e.type === "agent.delta" && e.detail?.kind === "reasoning") {
      const rec=S.think.get(e.actor) || {chars:0,tail:""};
      const text=e.detail.text || "";
      rec.chars+=text.length;rec.tail=(rec.tail+text).slice(-6000);S.think.set(e.actor,rec);
    }
    const visible=[...task.segments.values()].filter(hasNarrative);
    const needsTaskRow=(task.started || task.title || [...task.tools.values()].some(tool=>!wrapper(tool.name))) && !rows.get(task.key)?.isConnected;
    if(needsTaskRow || visible.some(segment=>!segmentRows.get(segment.key)?.isConnected)
        || terminal.has(task.status) && outcomeText(task) && !summaryRows.get(task.key)?.isConnected) render(task);
    else if(visible.length || rows.get(task.key)?.isConnected) {
      dirty.add(task.key);
      if(timer === null) timer=setTimeout(()=>{
        timer=null;
        for(const key of dirty) {const current=store.tasks.get(key);if(current) render(current);}
        dirty.clear();
      },100);
    }
    // 与历史窗口一起释放已移出页面的任务，不影响仍在阅读的折叠块。
    if(rows.size>250) for(const [key,row] of rows) if(!row.isConnected) {
      if([...taskSegments(key)].some(segmentRow=>segmentRow.isConnected) || summaryRows.get(key)?.isConnected) continue;
      rows.delete(key);store.tasks.delete(key);
      for(const [id,segmentRow] of segmentRows) if(!segmentRow.isConnected) segmentRows.delete(id);
      summaryRows.delete(key);}
    return true;
  }
  const taskSegments=key=>[...(store.tasks.get(key)?.segments.values() || [])].map(segment=>segmentRows.get(segment.key)).filter(Boolean);
  function routeSnapshot() {
    return [...store.tasks.values()].flatMap(task=>[...task.steps.values()].flatMap(step=>{
      const text=[...step.texts.values()].map(commentaryText).filter(Boolean).at(-1);
      return text ? [{id:`__process_route:${JSON.stringify([task.key,step.key])}`, kind:"milestone",
        title:brief(text.split(/\n/)[0]), body:text, task_id:task.id, route_id:step.route,
        turn:step.turn, actor:task.actor, status:step.status, exploration:step.exploration, evidence_refs:[], parent_finding_id:""}] : [];
    }));
  }
  function progressSnapshot() {
    return [...store.tasks.values()].filter(task=>task.id).map(task=>({
      task_id:task.id, actor:task.actor, title:task.title, status:task.status,
      text:liveSummary(task), waiting:task.waitingReason || "", updated:task.updated}));
  }
  function reset() {
    if(timer !== null) clearTimeout(timer);
    timer=null;dirty.clear();store=create();rows.clear();segmentRows.clear();summaryRows.clear();
    timelineEpoch=0;activeIntentKey=null;
  }
  function flush() {
    if(timer !== null) clearTimeout(timer);
    timer=null;
    for(const key of dirty) {const task=store.tasks.get(key);if(task) render(task);}
    dirty.clear();
  }
  return {push,reset,flush,create,reduce,summaryOf,resolve,updatesOf,commentaryText,parsePublicProgress,narrativeOf,skillsOf,stepTitle,stageSummary,liveSummary,progressSnapshot,routeSnapshot,actorLabel};
})();
