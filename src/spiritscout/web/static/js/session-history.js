"use strict";
/* 首屏只读最近一页；历史按字节游标翻页，实时事件分帧绘制。 */
const SessionHistory = (() => {
  let version = 0, past = false, page = null, trail = [], busy = false;
  let queue = [], head = 0, frame = null, waiters = [], controller = null;
  const paint = () => new Promise(resolve => requestAnimationFrame(resolve));
  const active = (id, token) => S.cur === id && S.view === "session" && version === token;
  function controls(message = "") {
    const root = $("#trace-history");
    if (!root) return;
    root.querySelector("[data-history-status]").textContent = message ||
      `${past ? "历史记录" : "最近记录"} · 本页 ${page?.activity_count ?? page?.items.length ?? 0} 项活动${page?.has_more ? " · 可向前翻页" : ""}`;
    root.querySelector('[data-history="older"]').disabled = busy || !page?.has_more;
    root.querySelector('[data-history="newer"]').disabled = busy || !trail.length;
    root.querySelector('[data-history="latest"]').disabled = busy;
  }
  function clearLiveQueue() {
    if (frame !== null) cancelAnimationFrame(frame);
    frame = null; queue = []; head = 0;
    const done = waiters; waiters = []; done.forEach(resolve => resolve());
  }
  function flush() {
    frame = null;
    const start = performance.now();
    while (head < queue.length && performance.now() - start < 6) {
      const item = queue[head++];
      if (S.cur !== item.session || S.view !== "session" || past) continue;
      renderAuditEvent(item.event);
      S.evSeen = item.id || S.evSeen;
    }
    // 长时间开着的会话也限制展示窗口；完整内容仍可在历史页和审计中查看。
    const box = $("#trace-box");
    while (box.children.length > 250) box.firstElementChild.remove();
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
  async function load(before = null, nextTrail = []) {
    const token = ++version, session = S.cur;
    controller?.abort(); controller = new AbortController();
    stopStreams(); busy = true; past = before !== null;
    controls("正在读取记录…");
    traceReset(); ToolParameters.reset(session); $("#events").replaceChildren();
    try {
      const suffix = `?activity=true${before === null ? "" : `&before=${before}`}`;
      const result = await api(`/api/sessions/${session}/audit-page${suffix}`, {signal: controller.signal});
      if (!active(session, token)) return;
      let deadline = performance.now() + 6;
      for (const item of result.items) {
        renderAuditEvent(item.event);
        if (performance.now() >= deadline) {
          await paint();
          if (!active(session, token)) return;
          deadline = performance.now() + 6;
        }
      }
      if (!active(session, token)) return;
      page = {...result, request_before: before}; trail = nextTrail; busy = false;
      S.evSeen = result.cursor;
      controls();
      traceScroll(true);
      if (!past) startStreams(false);
    } catch (error) {
      if (!active(session, token) || error.name === "AbortError") return;
      busy = false; page = null;
      controls(`读取失败：${error.message}，可点击“最近记录”重试`);
    }
  }
  function open() { page = null; trail = []; load(); }
  document.addEventListener("click", event => {
    const button = event.target.closest("[data-history]");
    if (!button || button.disabled || busy) return;
    if (button.dataset.history === "older" && page?.has_more)
      load(page.older_cursor, [...trail, past ? page.request_before : null]);
    else if (button.dataset.history === "newer" && trail.length)
      load(trail[trail.length - 1], trail.slice(0, -1));
    else if (button.dataset.history === "latest") load();
  });
  return {open, enqueue, drain, clearLiveQueue, isPast: () => past,
          cancel() { ++version; controller?.abort(); clearLiveQueue(); }};
})();
