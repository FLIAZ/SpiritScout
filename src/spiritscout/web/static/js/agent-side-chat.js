"use strict";
/* 旁路有自己的会话与消息历史；切换右栏不触碰主会话的 SSE、轨迹或输入框。 */
const AgentSideChat = (() => {
  let parent="", selected="", agents=[], session=null, generation=0, timer=null;
  const entries=new Map();
  const current=()=>entries.get(`${parent}:${selected}`);
  const visible=()=>S.view==="session" && S.tab==="chat" && S.cur===parent && !!selected;
  const allowed=()=>["src","ctf"].includes(session?.mode) && !session?.side_session;
  function entry(actor) {
    const key=`${parent}:${actor}`;
    if(!entries.has(key)) entries.set(key,{actor,task:"",child:"",draft:"",messages:[],busy:false,status:"",error:"",pending:[],questions:[]});
    return entries.get(key);
  }
  function linked(actor) {
    return (S.sessions || []).find(s=>s.side_session?.parent_session_id===parent && s.side_session?.actor===actor);
  }
  function cancel() { generation++;clearTimeout(timer);timer=null; }
  function sync(nextAgents,nextSession) {
    if(parent!==S.cur) {cancel();parent=S.cur;selected="";}
    agents=nextAgents;session=nextSession;
    if(!allowed() && selected) {cancel();selected="";}
    renderTabs();
    if(selected) {render();if(timer===null && !current()?.loading) refresh();}
  }
  function renderTabs() {
    const nav=$("#agent-chat-tabs");if(!nav)return;
    const items=allowed() ? agents.filter(a=>a.taskId) : [];
    const signature=JSON.stringify([parent,selected,items.map(a=>[a.actor,a.status])]);
    if(nav.dataset.signature!==signature) {
      nav.dataset.signature=signature;
      const focus=document.activeElement?.dataset.sideActor;
      nav.innerHTML=`<button type="button" data-side-actor="" aria-pressed="${!selected}">实况</button>`+items.map(a=>`<button type="button" data-side-actor="${esc(a.actor)}" aria-pressed="${a.actor===selected}" title="${esc(a.actor)} · ${esc(a.status)}">${esc(actorName(a.actor))}</button>`).join("");
      if(focus!==undefined) [...nav.querySelectorAll("button")].find(b=>b.dataset.sideActor===focus)?.focus({preventScroll:true});
    }
    $("#session-observer").hidden=!!selected;
    $("#agent-side-chat").hidden=!selected;
    $(".session-rail").classList.toggle("chat-open",!!selected);
  }
  function select(actor,task="") {
    if(actor && (!allowed() || !agents.some(a=>a.actor===actor && a.taskId))) return;
    cancel();selected=actor;renderTabs();if(!actor)return;
    const e=entry(actor), source=agents.find(a=>a.actor===actor);
    if(!e.child) e.child=linked(actor)?.session_id || "";
    if(!e.child && task) e.task=task;
    if(!e.task) e.task=source.taskId;
    const box=$("#agent-side-chat");delete box.querySelector(".side-chat-messages").dataset.signature;
    box.querySelector("textarea").value=e.draft;
    render();refresh();box.querySelector("textarea").focus();
  }
  function render() {
    const e=current(),box=$("#agent-side-chat");if(!e || !box)return;
    box.querySelector("[data-side-name]").textContent=actorName(e.actor);
    const expand=box.querySelector("[data-side-expand]");expand.hidden=!e.child;
    const task=box.querySelector("[data-side-task]"), source=agents.find(a=>a.actor===selected);
    const tasks=[...(source?.tasks?.values() || [])];
    if(e.task && !tasks.some(t=>t.id===e.task)) tasks.push({id:e.task});
    const taskSignature=JSON.stringify(tasks);
    if(task.dataset.signature!==taskSignature) {task.dataset.signature=taskSignature;task.innerHTML=tasks.map(t=>`<option value="${esc(t.id)}">${esc(t.title || t.id)}</option>`).join("");}
    task.value=e.task;task.disabled=!!e.child || e.busy;
    if(typeof selxSync==="function") selxSync(task);
    const messages=box.querySelector(".side-chat-messages"), signature=JSON.stringify(e.messages);
    if(messages.dataset.signature!==signature) {
      const stick=messages.scrollHeight-messages.scrollTop-messages.clientHeight<48 || !messages.dataset.signature;
      messages.dataset.signature=signature;
      messages.innerHTML=e.messages.length ? e.messages.map(m=>`<article class="side-message ${m.role==="user" ? "from-user" : "from-agent"}"><small>${m.role==="user" ? "你" : esc(actorName(e.actor))}</small><div class="md">${mdRender(m.content || "")}</div></article>`).join("") : '<div class="side-chat-empty"><strong>和子 Agent 直接对话</strong><p>发送第一条消息，开始独立的旁路会话。它会带入所选任务的已有记录。</p></div>';
      if(stick) messages.scrollTop=messages.scrollHeight;
    }
    box.querySelector("[data-side-status]").textContent=e.busy ? "正在发送…" : e.pending.length ? "有操作等待你确认" : e.questions.length ? "子 Agent 有问题需要你回答" : ["running","queued"].includes(e.status) ? "子 Agent 正在处理，新消息会在这里显示…" : e.status==="failed" ? "本轮回复失败，可补充消息重试或展开会话查看详情。" : e.status==="cancelled" ? "本轮已停止，可继续发送消息。" : "";
    box.querySelector("[data-side-error]").textContent=e.error;
    box.querySelector('[type="submit"]').disabled=e.busy;
    const pending=box.querySelector("[data-side-pending]");
    const pendingSignature=JSON.stringify([e.pending,e.questions]);
    if(pending.dataset.signature!==pendingSignature) {
      pending.dataset.signature=pendingSignature;
      pending.innerHTML=e.pending.length || e.questions.length ? '<button class="btn sm" type="button" data-side-review>打开会话处理审批与提问</button>' : "";
    }
  }
  async function refresh() {
    const e=current(),version=generation;
    clearTimeout(timer);timer=null;
    if(!visible() || !e?.child || e.busy || e.loading)return;
    e.loading=true;
    try {
      const data=await api(`/api/sessions/${encodeURIComponent(e.child)}/conversation`);
      if(version!==generation || !visible())return;
      e.messages=data.messages.filter(m=>["user","assistant"].includes(m.role));
      e.status=data.session.status;e.pending=data.pending_approvals || [];e.questions=data.pending_questions || [];
      if(data.session.error) e.error=data.session.error;
      if(data.session.side_session?.task_id) e.task=data.session.side_session.task_id;
      render();
    } catch(error) {if(version!==generation)return;e.error=error.message;render();}
    finally {e.loading=false;}
    if(version===generation && visible()) timer=setTimeout(refresh,["completed","failed","cancelled"].includes(e.status) ? 5000 : 1000);
  }
  async function send(event) {
    event.preventDefault();const e=current();if(!e || e.busy || !allowed())return;
    const mission=e.draft.trim();if(!mission)return;
    const origin=parent;cancel();
    e.busy=true;e.error="";render();
    try {
      if(!e.child) {
        const child=await api(`/api/sessions/${encodeURIComponent(origin)}/agent-sessions`,{method:"POST",body:JSON.stringify({actor:e.actor,task_id:e.task,mission})});
        e.child=child.session_id;S.sessions.unshift(child);
      } else await api(`/api/sessions/${encodeURIComponent(e.child)}/messages`,{method:"POST",body:JSON.stringify({content:mission})});
      e.messages.push({role:"user",content:mission});e.status="running";e.draft="";
      if(current()===e && S.cur===origin) $("#agent-side-chat textarea").value="";
    } catch(error) {e.error=error.message;}
    finally {e.busy=false;if(current()===e && S.cur===origin){render();refresh();}}
  }
  function init() {
    $("#agent-chat-tabs").addEventListener("click",event=>{const button=event.target.closest("[data-side-actor]");if(button)select(button.dataset.sideActor);});
    const box=$("#agent-side-chat");box.querySelector("form").addEventListener("submit",send);
    box.querySelector("textarea").addEventListener("input",event=>{const e=current();if(e)e.draft=event.target.value;});
    box.querySelector("textarea").addEventListener("keydown",event=>{if(event.key==="Enter" && (event.ctrlKey || event.metaKey) && !event.isComposing){event.preventDefault();box.querySelector("form").requestSubmit();}});
    box.querySelector("[data-side-task]").addEventListener("change",event=>{const e=current();if(e && !e.child)e.task=event.target.value;});
    box.addEventListener("click",event=>{if(event.target.closest("[data-side-expand],[data-side-review]") && current()?.child)openSession(current().child);});
  }
  return {sync,select,init};
})();
