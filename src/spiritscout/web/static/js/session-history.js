"use strict";
/* 连续滚动历史：只保存三段原始事件及其渲染状态，移出的内容按字节游标重读。 */
const SessionHistory = (() => {
  const WINDOW = 3, PAGE_SIZE = 160;
  let version = 0, pages = [], first = 0, last = -1, busy = false, past = false;
  let queue = [], head = 0, frame = null, waiters = [], controller = null;
  let scrollFrame = null, ignoreScroll = false, pending = false, error = "", retryDirection = -1;
  const box = () => $("#trace-box");
  const paint = () => new Promise(resolve => requestAnimationFrame(resolve));
  const active = (id, token) => S.cur === id && S.view === "session" && version === token;
  function controls(message = "") {
    const root = $("#trace-history");
    if (!root) return;
    root.querySelector("[data-history-status]").textContent = message || error ||
      (busy ? "正在读取记录…" : pending ? "有新消息 · 回到最新即可继续跟随" :
        pages[first]?.has_more || first > 0 ? "向上滚动查看更早记录" : "已到会话开头");
    root.querySelector('[data-history="latest"]').disabled = busy;
    const retry = root.querySelector('[data-history="retry"]');
    if (retry) { retry.hidden = !error; retry.disabled = busy; }
  }
  function clearLiveQueue() {
    if (frame !== null) cancelAnimationFrame(frame);
    frame = null; queue = []; head = 0;
    const done = waiters; waiters = []; done.forEach(resolve => resolve());
  }
  function remember(page, item) {
    const previous = page.items.at(-1), e = item.event, d = e.detail || {}, p = previous?.event;
    // 原文片段只合并连续同一次调用的同类 delta；与服务端历史投影一致。
    if (e.type === "agent.delta" && p?.type === e.type && e.actor === p.actor &&
        ["text", "reasoning"].includes(d.kind) &&
        ["kind", "call_id", "route", "turn", "task_id", "route_id"].every(k => d[k] === p.detail?.[k])) {
      previous.id = item.id;
      previous.event = {...e, detail:{...d, text:String(p.detail.text || "") + String(d.text || "")}};
    } else page.items.push(item);
    if (e.type !== "agent.delta") page.activity_count++;
    page.cursor = item.id || page.cursor;
  }
  function flush() {
    frame = null;
    // 实时窗口轮换期间保留队列，待原子替换完成再消费；不能误当作回看历史而跳过。
    if (busy && !past) return;
    const start = performance.now();
    while (head < queue.length && performance.now() - start < 6) {
      const item = queue[head++];
      if (S.cur !== item.session || S.view !== "session") continue;
      if (past || busy) {
        SessionObserver.push(item.event); AgentWorkbench.push(item.event);
        pending = true; continue;
      }
      let page = pages[last];
      if (!page) continue;
      if (page.activity_count >= PAGE_SIZE || page.items.length >= 800) {
        pages.push({items:[], cursor:page.cursor, start:page.cursor, has_more:true, activity_count:0});
        last++; first = Math.max(0, last - WINDOW + 1);
        for (let i = 0; i < first; i++) pages[i].items = null;
        // 仅跟随最新时滑动窗口；历史阅读期间不删除用户正在看的内容。
        busy = true;
        const session = S.cur, token = version;
        redraw(session, token).finally(() => {
          if (!active(session, token)) return;
          busy = false; controls();
          if (head < queue.length && frame === null) frame = requestAnimationFrame(flush);
        });
        head--; return;
      }
      remember(page, item);
      const previous = box().lastElementChild;
      renderAuditEvent(item.event);
      const row = box().lastElementChild;
      if (row && row !== previous) row.dataset.historyId = String(item.id);
      S.evSeen = item.id || S.evSeen;
    }
    controls();
    if (head < queue.length) {
      if (head > 2048) { queue = queue.slice(head); head = 0; }
      frame = requestAnimationFrame(flush);
    } else { queue = []; head = 0; const done = waiters; waiters = []; done.forEach(resolve => resolve()); }
  }
  function enqueue(event, id, session) {
    queue.push({event, id, session});
    if (frame === null) frame = requestAnimationFrame(flush);
  }
  function drain() {
    return head < queue.length ? new Promise(resolve => waiters.push(resolve)) : Promise.resolve();
  }
  function identity(row) {
    if (row.dataset.taskId) return JSON.stringify([row.dataset.agent, row.dataset.taskId]);
    return JSON.stringify([row.dataset.agent, row.dataset.taskId, row.dataset.eventTs, row.dataset.historyId]);
  }
  function position() {
    const top = box().getBoundingClientRect().top;
    const row = [...box().children].find(el => el.getBoundingClientRect().bottom > top + 1 ||
      (el.getBoundingClientRect().bottom === undefined && el.getBoundingClientRect().top >= top));
    return row ? {key:identity(row), offset:row.getBoundingClientRect().top - top} : null;
  }
  async function redraw(session, token, {anchor = null, follow = true, observe = false} = {}) {
    const opened = new Set(), tools = new Set();
    const toolKey = el => el.dataset.toolKey || el.dataset.id;
    const detailKey = (row, el, i) => `${identity(row)}:${toolKey(el.closest?.('.tl-act') || el) || i}:${el.className}`;
    for (const row of box().children) {
      row.querySelectorAll("details").forEach((el, i) => {
        if (el.open) opened.add(detailKey(row, el, i));
      });
      row.querySelectorAll(".tl-act.open").forEach(el => tools.add(`${identity(row)}:${toolKey(el)}`));
    }
    ignoreScroll = true;
    const stage = typeof traceBeginRender === "function" ? traceBeginRender() : null;
    const targetBox = stage || box();
    let complete = false;
    try {
      traceReset({preservePanels:!observe}); traceStick = false;
      let deadline = performance.now() + 6;
      for (let i = first; i <= last; i++) {
        for (const item of pages[i].items || []) {
          const previous = targetBox.lastElementChild;
          renderAuditEvent(item.event, {observe});
          const row = targetBox.lastElementChild;
          if (row && row !== previous) row.dataset.historyId = String(item.id);
          if (performance.now() >= deadline) {
            await paint();
            if (!active(session, token)) return;
            deadline = performance.now() + 6;
          }
        }
      }
      // 聚合卡片先完成布局，再一次替换旧窗口，分帧构建期间保留用户当前画面。
      if (typeof TaskTrace !== "undefined") TaskTrace.flush();
      complete = true;
    } finally {
      if (stage) traceEndRender(stage, complete && active(session, token));
    }
    for (const row of box().children) {
      row.querySelectorAll("details").forEach((el, i) => {
        if (opened.has(detailKey(row, el, i))) el.open = true;
      });
      row.querySelectorAll(".tl-act").forEach(el => {
        if (!tools.has(`${identity(row)}:${toolKey(el)}`)) return;
        el.classList.add("open"); el.querySelector("button.hd").setAttribute("aria-expanded", "true");
      });
    }
    ToolParameters.prune?.();
    const target = anchor && [...box().children].find(el => identity(el) === anchor.key);
    if (target) box().scrollTop += target.getBoundingClientRect().top - box().getBoundingClientRect().top - anchor.offset;
    else if (follow) { box().scrollTop = box().scrollHeight; traceStick = true; }
    else box().scrollTop = 0;
    await paint();
    if (!active(session, token)) return;
    ignoreScroll = false;
    traceScroll();
  }
  async function read(before = null) {
    controller?.abort(); controller = new AbortController();
    return api(`/api/sessions/${S.cur}/audit-page?activity=true&limit=${PAGE_SIZE}` +
      (before === null ? "" : `&before=${before}`), {signal:controller.signal});
  }
  async function latest({initial = false} = {}) {
    const session = S.cur, token = ++version;
    stopStreams(); busy = true; error = ""; pending = false; controls();
    retryDirection = 0;
    if (initial) {
      pages = []; first = 0; last = -1; past = false;
      box().replaceChildren(); $("#events").replaceChildren();
      ToolParameters.reset(session);
    }
    try {
      const result = await read();
      if (!active(session, token)) return;
      pages = [{...result, start:result.older_cursor}]; first = last = 0; past = false;
      await redraw(session, token, {observe:initial});
      if (!active(session, token)) return;
      S.evSeen = result.cursor; busy = false; controls(); startStreams(false);
    } catch (e) {
      if (!active(session, token) || e.name === "AbortError") return;
      busy = false; ignoreScroll = false; error = `读取失败：${e.message}`; controls();
      if (pages.length) startStreams(false);
    }
  }
  async function move(direction) {
    if (busy || !pages.length) return;
    const session = S.cur, token = version, anchor = position();
    const old = {first,last,past};
    let index = direction < 0 ? first - 1 : last + 1;
    if (index < 0 && !pages[0].has_more) return;
    if (index >= pages.length) {
      if (past) { past = false; pending = false; controls(); startStreams(false); }
      return;
    }
    busy = true; past = true; error = ""; controls();
    retryDirection = direction;
    try {
      let page = index < 0 ? null : pages[index];
      if (!page?.items) {
        const result = await read(page ? page.cursor : pages[0].older_cursor);
        if (!active(session, token)) return;
        if (page) page.items = result.items.filter(item => item.id > page.start);
        else { page = {...result, start:result.older_cursor}; }
      }
      if (!active(session, token)) return;
      if (index < 0) { pages.unshift(page); first++; last++; index = 0; }
      if (direction < 0) { first = index; last = Math.min(last, first + WINDOW - 1); }
      else { last = index; first = Math.max(first, last - WINDOW + 1); }
      for (let i = 0; i < pages.length; i++) if (i < first || i > last) pages[i].items = null;
      await redraw(session, token, {anchor, follow:false});
      if (!active(session, token)) return;
      const atEnd = last === pages.length - 1 && box().scrollHeight - box().scrollTop - box().clientHeight < 180;
      past = !atEnd;
      if (atEnd) pending = false;
      busy = false; controls();
      if (last === pages.length - 1 && !past) startStreams(false);
    } catch (e) {
      if (!active(session, token) || e.name === "AbortError") return;
      first = old.first; last = old.last; past = old.past || pending;
      busy = false; ignoreScroll = false; error = `读取失败：${e.message}，继续滚动或点击重试`; controls();
    }
  }
  function onScroll() {
    if (busy || ignoreScroll || S.view !== "session") return;
    if (!traceStick) past = true;
    if (scrollFrame !== null) return;
    scrollFrame = requestAnimationFrame(() => {
      scrollFrame = null;
      if (busy || ignoreScroll) return;
      if (box().scrollTop < 180) move(-1);
      else if (box().scrollHeight - box().scrollTop - box().clientHeight < 180) move(1);
    });
  }
  box().addEventListener("scroll", onScroll, {passive:true});
  document.addEventListener("click", event => {
    const button = event.target.closest("[data-history]");
    if (!button || button.disabled || busy) return;
    if (button.dataset.history === "latest") latest();
    else if (button.dataset.history === "retry") retryDirection ? move(retryDirection) : latest({initial:!pages.length});
  });
  return {open:() => latest({initial:true}), latest, enqueue, drain, clearLiveQueue, isPast:() => past,
    isRendering:() => busy || ignoreScroll,
    cancel() {
      ++version; controller?.abort(); clearLiveQueue();
      if (scrollFrame !== null) cancelAnimationFrame(scrollFrame);
      scrollFrame = null; pages = []; first = 0; last = -1; busy = false; past = false; ignoreScroll = false;
    }};
})();
