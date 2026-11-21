"use strict";
/* 审计游标只负责取数；连续消息由虚拟列表保留身份、交互状态与测量高度。 */
/** @typedef {import('./frontend-types').AuditItem} AuditItem */
/** @typedef {import('./frontend-types').HistoryPage} HistoryPage */
/** @typedef {import('./frontend-types').AuditPage & {touched?: number}} CachedAuditPage */
const SessionHistory = (() => {
  const PAGE_SIZE = 160, CACHE_PAGES = 12;
  let version = 0, pages = /** @type {HistoryPage[]} */ ([]), first = 0, last = -1, busy = false, past = false;
  let queue = /** @type {{event: import('./frontend-types').AuditEvent, id:number, session:string}[]} */ ([]), head = 0, frame = null, waiters = [], controller = null;
  let scrollFrame = null, ignoreScroll = false, pending = false, error = "", retryDirection = -1;
  let scrollTop = 0, scrollDirection = 0, cacheClock = 0, retryKind = "", retryIds = null;
  let syncing = false, syncController = null;
  const box = () => $("#trace-box");
  const viewport = () => typeof window !== "undefined" ? window.TraceViewport : null;
  const traceRows = () => viewport()?.rows() || [...box().children];
  /** @returns {Promise<number>} */
  const paint = () => new Promise(resolve => requestAnimationFrame(resolve));
  const active = (id, token) => S.cur === id && S.view === "session" && version === token;
  function controls(message = "") {
    const root = $("#trace-history");
    if (!root) return;
    root.querySelector("[data-history-status]").textContent = message || error ||
      (busy ? I18n.t("正在读取记录…") : pending ? I18n.t("有新消息 · 回到最新即可继续跟随") :
        pages[first]?.has_more || first > 0 ? I18n.t("向上滚动查看更早记录") : I18n.t("已到会话开头"));
    root.querySelector('[data-history="latest"]').disabled = busy;
    const oldestButton = root.querySelector('[data-history="oldest"]');
    if (oldestButton) oldestButton.disabled = busy || !pages.length || (!pages[0].has_more && first === 0);
    const retry = root.querySelector('[data-history="retry"]');
    if (retry) { retry.hidden = !error; retry.disabled = busy; }
    refreshAnchors();
  }
  // 用户消息目录独立于滚动窗口；同次发送只按消息信封 ID 分组。
  let anchorKey = null, anchorIndex = null, previewBar = null, anchorJump = 0, selectedAnchor = null;
  let renderedSession = null;
  const anchorRows = () => traceRows().filter(el => el.classList.contains("tl-user") && el.dataset.historyId);
  const messageKey = (detail, id) => detail?.message_id
    ? JSON.stringify(["message", String(detail.message_id)]) : JSON.stringify(["event", String(id)]);
  function anchorText(row) {
    const text = row.querySelector(".btxt")?.textContent.trim();
    if (text) return text;
    const name = row.querySelector(".fc-name")?.textContent.trim();
    return name ? I18n.t`[附件] ${name}` : I18n.t("用户消息");
  }
  function groupsFromEvents(items) {
    const groups = [], byKey = new Map();
    for (const item of [...items].sort((a, b) => Number(a.id) - Number(b.id))) {
      const kind = item.event?.type, d = item.event?.detail || {};
      if (!["user.message", "user.answered", "user.attachment"].includes(kind)) continue;
      if (kind === "user.answered" && !String(d.answer || "").trim()) continue;
      const key = messageKey(d, item.id);
      let group = byKey.get(key);
      if (!group) { group = {key, ids: [], parts: []}; byKey.set(key, group); groups.push(group); }
      group.ids.push(String(item.id));
      group.parts.push(kind === "user.answered" ? String(d.answer || "").trim() : kind === "user.message" ? String(d.content || "").trim() : I18n.t`[附件] ${d.name || I18n.t("附件")}`);
    }
    return groups;
  }
  function indexForSession() {
    if (!S.cur) return null;
    if (anchorIndex?.session !== S.cur) {
      anchorIndex?.controller?.abort();
      anchorIndex = {session: S.cur, events: new Map(), revision: 0, groupsRevision: -1,
        groups: [], before: null, loading: false, complete: false, error: "", controller: null};
      anchorKey = null;
    }
    return anchorIndex;
  }
  function recordAnchor(item, state = indexForSession()) {
    if (!state || !item.id || !["user.message", "user.answered", "user.attachment"].includes(item.event?.type)) return;
    const id = String(item.id), old = state.events.get(id);
    if (old && JSON.stringify(old.event) === JSON.stringify(item.event)) return;
    state.events.set(id, item); state.revision++;
  }
  async function loadAnchorIndex({retry = false} = {}) {
    const state = indexForSession();
    if (!state || state.loading || state.complete || (state.error && !retry)) return;
    state.loading = true; state.error = ""; state.controller = new AbortController();
    refreshAnchors();
    try {
      let before = state.before;
      while (anchorIndex === state && S.cur === state.session) {
        const result = await api(`/api/sessions/${state.session}/audit-page?types=user.message,user.answered,user.attachment&limit=250` +
          (before === null ? "" : `&before=${before}`), {signal: state.controller.signal});
        if (anchorIndex !== state || S.cur !== state.session) return;
        for (const item of result.items) recordAnchor(item, state);
        const next = Number(result.older_cursor);
        if (result.has_more && (!Number.isFinite(next) || next <= 0 || (before !== null && next >= before)))
          throw new Error(I18n.t("存档点分页游标未向前推进"));
        state.before = next;
        if (!result.has_more) { state.complete = true; break; }
        before = next;
        refreshAnchors();
      }
    } catch (e) {
      if (anchorIndex === state && e.name !== "AbortError") state.error = e.message || I18n.t("存档点读取失败");
    } finally {
      if (anchorIndex === state) { state.loading = false; refreshAnchors(); }
    }
  }
  function refreshAnchors() {
    const root = $("#um-rail"), state = indexForSession();
    if (!root || !state) return;
    // 兼容由旧渲染入口产生的行；完整事件目录优先，不用 DOM 文本覆盖原文。
    for (const row of renderedSession === state.session ? anchorRows() : []) if (!state.events.has(row.dataset.historyId)) {
      const file = row.querySelector(".fc-name");
      recordAnchor({id: Number(row.dataset.historyId), event: {type: file ? "user.attachment" : "user.message",
        detail: {message_id: row.dataset.messageId, ...(file ? {name: file.textContent.trim()} : {content: anchorText(row)})}}}, state);
    }
    if (state.groupsRevision !== state.revision) {
      state.groups = groupsFromEvents(state.events.values()); state.groupsRevision = state.revision;
    }
    root.hidden = !state.groups.length && !state.loading && !state.error;
    root.setAttribute("aria-busy", String(state.loading));
    const key = JSON.stringify([state.revision, state.loading, state.error, busy]);
    if (key !== anchorKey) {
      anchorKey = key;
      const existing = new Map([...root.children].filter(el => el.dataset.anchorGroup).map(el => [el.dataset.anchorGroup, el]));
      const bars = state.groups.map(group => {
        let el = existing.get(group.key);
        if (!el) {
          el = document.createElement("button"); el.type = "button"; el.className = "um-bar";
          el.dataset.anchorGroup = group.key;
          el.addEventListener("click", async () => {
            if (busy) return;
            hideAnchorPreview();
            const ids = el.dataset.ids.split("|");
            const target = anchorRows().find(row => ids.includes(row.dataset.historyId));
            if (target) await jumpToAnchor(target); else await locate(ids);
          });
          el.addEventListener("mouseenter", () => showAnchorPreview(el));
          el.addEventListener("mouseleave", hideAnchorPreview);
        }
        el.disabled = busy; el.dataset.historyId = group.ids[0]; el.dataset.ids = group.ids.join("|");
        const text = group.parts.filter(Boolean).join("\n").slice(0, 400) || I18n.t("用户消息");
        el.dataset.preview = text; el.setAttribute("aria-label", I18n.t`回到这条消息：${text.slice(0, 60)}`);
        return el;
      });
      if (state.error || (state.loading && !bars.length)) {
        const button = document.createElement("button"); button.type = "button"; button.className = "btn sm";
        button.textContent = state.loading ? "…" : "↻"; button.disabled = state.loading;
        button.title = state.error ? I18n.t`存档点未补全：${state.error}，点击重试` : I18n.t("正在补全会话存档点");
        button.setAttribute("aria-label", state.error ? I18n.t("重试加载会话存档点") : I18n.t("正在补全会话存档点"));
        button.style.padding = "0"; button.style.width = "24px"; button.style.height = "22px";
        button.addEventListener("click", () => loadAnchorIndex({retry: true})); bars.push(button);
      }
      root.replaceChildren(...bars);
      if (previewBar?.isConnected) showAnchorPreview(previewBar); else hideAnchorPreview();
    }
    markActiveAnchor();
    if (!state.loading && !state.complete && !state.error) loadAnchorIndex();
  }
  function markActiveAnchor() {
    const root = $("#um-rail"), state = indexForSession();
    if (!root || root.hidden || !state) return;
    let key = selectedAnchor;
    if (!key) {
      const boundary = box().getBoundingClientRect().top + 13;
      const rows = (viewport()?.mounted() || [...box().children]).filter(row => visibleRect(row));
      const row = rows.filter(row => row.getBoundingClientRect().top <= boundary).at(-1) || rows[0];
      let ref = Number(row?.dataset.historyStart || row?.dataset.historyId);
      if (!Number.isFinite(ref)) return;
      if (row.classList.contains("tl-user") && row.getBoundingClientRect().top > boundary) ref--;
      for (const group of state.groups) {
        if (Number(group.ids[0]) > ref) break;
        key = group.key;
      }
    }
    for (const bar of root.children) if (bar.dataset.anchorGroup)
      bar.classList.toggle("active", bar.dataset.anchorGroup === key);
  }
  async function jumpToAnchor(row) {
    const session = S.cur, token = version, jump = ++anchorJump;
    selectedAnchor = anchorIndex?.groups.find(group => group.ids.includes(row.dataset.historyId))?.key || null;
    ignoreScroll = true;
    traceStick = false; past = true;
    let found = true;
    try {
      const view = viewport();
      if (view) { view.stop(); found = await view.navigate(row); }
      else box().scrollTop += row.getBoundingClientRect().top - box().getBoundingClientRect().top - 12;
      await paint();
    } finally {
      if (active(session, token) && jump === anchorJump) {
        if (!found) selectedAnchor = null;
        ignoreScroll = false; scrollTop = box().scrollTop;
        controls(found ? "" : I18n.t("当前记录中未显示这条消息"));
      }
    }
    return found;
  }
  async function locate(ids) {
    const session = S.cur;
    await navigate(async () => {
      const numbers = ids.map(Number).filter(Number.isFinite);
      if (!numbers.length) throw new Error(I18n.t("消息游标无效"));
      // 新消息可能在回看期间到达：补取最新页后仍保持回看，不偷换为跟随模式。
      if (Math.max(...numbers) > pages.at(-1).cursor) {
        const result = await read();
        const page = {...result, start:pages.at(-1).cursor, items:result.items.filter(item=>item.id>pages.at(-1).cursor)};
        await renderPage(page, session, version, {mode:"append"});
        pages.push(page); last++;
      }
      let index = pageForIds(ids);
      while (index < 0 && pages[0].has_more) { await prependPage(); index = pageForIds(ids); }
      if (index < 0) throw new Error(I18n.t("未找到这条消息，请刷新存档点后重试"));
      return index;
    }, I18n.t("正在定位消息…"), ids);
  }
  function showAnchorPreview(bar) {
    const tip = $("#um-preview"), wrap = bar.closest(".trace-wrap");
    if (!tip || !wrap) return;
    previewBar = bar; tip.textContent = bar.dataset.preview.slice(0, 400); tip.hidden = false;
    const r = bar.getBoundingClientRect(), w = wrap.getBoundingClientRect();
    tip.style.left = `${Math.max(r.right - w.left + 10, 10)}px`;
    const top = r.top - w.top - tip.offsetHeight / 2 + 3;
    tip.style.top = `${Math.min(Math.max(top, 8), Math.max(w.height - tip.offsetHeight - 8, 8))}px`;
  }
  function hideAnchorPreview() { previewBar = null; const tip = $("#um-preview"); if (tip) tip.hidden = true; }
  function clearLiveQueue() {
    if (frame !== null) cancelAnimationFrame(frame);
    frame = null; queue = []; head = 0;
    const done = waiters; waiters = []; done.forEach(resolve => resolve());
  }
  /**
   * @param {HistoryPage} page
   * @param {AuditItem} item
   */
  function remember(page, item) {
    page.touched = ++cacheClock;
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
    if (busy) return;
    const start = performance.now();
    while (head < queue.length && performance.now() - start < 6) {
      const item = queue[head++];
      if (S.cur !== item.session || S.view !== "session") continue;
      if (past) pending = true;
      let page = pages[last];
      if (!page) continue;
      if (page.activity_count >= PAGE_SIZE || page.items.length >= 800) {
        pages.push({items:[], cursor:page.cursor, start:page.cursor, has_more:true, activity_count:0});
        last++; page = pages[last]; trimCache();
      }
      remember(page, item);
      const previous = traceRows().at(-1);
      renderAuditEvent(item.event, {eventId: item.id});
      const row = traceRows().at(-1);
      if (row && row !== previous) row.dataset.historyId = String(item.id);
      S.evSeen = item.id || S.evSeen;
    }
    controls();
    if (head < queue.length) {
      if (head > 2048) { queue = queue.slice(head); head = 0; }
      frame = requestAnimationFrame(flush);
    } else { queue = []; head = 0; const done = waiters; waiters = []; done.forEach(resolve => resolve()); }
  }
  /**
   * @param {import("./frontend-types").AuditEvent} event
   * @param {number} id
   * @param {string} session
   */
  function enqueue(event, id, session) {
    // 回看和重连清队列都不能丢掉新用户消息的存档点。
    if (session === S.cur && S.view === "session") recordAnchor({event, id});
    queue.push({event, id, session});
    if (frame === null) frame = requestAnimationFrame(flush);
  }
  function drain() {
    return head < queue.length ? new Promise(resolve => waiters.push(resolve)) : Promise.resolve();
  }
  function visibleRect(el, viewport = null) {
    if (el.checkVisibility && !el.checkVisibility({visibilityProperty: true, contentVisibilityAuto: true})) return null;
    // 关闭的 details 子树在 Chromium 中仍可能有布局矩形，不能凭矩形当作可见。
    for (let parent = el.parentElement; parent && parent !== box(); parent = parent.parentElement) {
      if (parent.hidden) return null;
      if (parent.tagName === "DETAILS" && !parent.open) {
        const summary = [...parent.children].find(child => child.tagName === "SUMMARY");
        if (!summary?.contains(el)) return null;
      }
    }
    const rect = el.getBoundingClientRect(), bottom = rect.bottom ?? rect.top + 100;
    if (bottom <= rect.top || rect.width === 0) return null;
    if (viewport && (rect.top >= viewport.bottom - 1 || bottom <= viewport.top + 1)) return null;
    return {...rect, top: rect.top, bottom};
  }
  function resumeQueue() {
    if (head < queue.length && frame === null) frame = requestAnimationFrame(flush);
  }
  async function renderPage(page, session, token, {mode = "prepend", observe = false} = {}) {
    const stage = typeof traceBeginRender === "function" ? traceBeginRender({isolate:mode === "prepend"}) : null;
    const oldTop = box().scrollTop, oldHeight = box().scrollHeight;
    let committed = false;
    try {
      traceReset({preservePanels:mode !== "replace" || !observe});
      traceStick = mode === "replace";
      let deadline = performance.now() + 6;
      for (const item of page.items || []) {
        const target = stage || box(), previous = target.lastElementChild;
        renderAuditEvent(item.event, {observe,eventId:item.id,animate:false});
        const row = target.lastElementChild;
        if (row && row !== previous && !row.dataset.historyId) row.dataset.historyId = String(item.id);
        if (performance.now() >= deadline) {
          await paint();
          if (!active(session, token)) return false;
          deadline = performance.now() + 6;
        }
      }
      if (typeof TaskTrace !== "undefined") TaskTrace.flush();
      committed = active(session, token);
    } finally {
      if (stage) traceEndRender(stage, committed, mode);
    }
    if (!committed) return false;
    if (mode === "replace") traceScroll(true);
    else if (!viewport() && stage) box().scrollTop = oldTop + box().scrollHeight - oldHeight;
    await paint();
    return active(session, token);
  }
  /**
   * @param {number | null} [before]
   * @returns {Promise<CachedAuditPage>}
   */
  async function read(before = null) {
    const session = S.cur, token = version;
    controller?.abort(); controller = new AbortController();
    /** @type {CachedAuditPage} */
    const result = await api(`/api/sessions/${session}/audit-page?activity=true&limit=${PAGE_SIZE}` +
      (before === null ? "" : `&before=${before}`), {signal: controller.signal});
    if (!active(session, token)) { const e = new Error(I18n.t("会话已切换")); e.name = "AbortError"; throw e; }
    for (const item of result.items) recordAnchor(item);
    result.touched = ++cacheClock;
    return result;
  }
  function trimCache() {
    const cached = pages.filter(page => page.items).sort((a,b) =>
      Number(b === pages[last]) - Number(a === pages[last]) || (b.touched || 0) - (a.touched || 0));
    for (const page of cached.slice(CACHE_PAGES)) page.items = null;
  }
  function pageForIds(ids) {
    return pages.findIndex(page => ids.some(id => Number(id) > page.start && Number(id) <= page.cursor));
  }
  async function prependPage() {
    const session = S.cur, token = version, before = pages[0].older_cursor;
    const result = await read(before);
    if (Number(result.older_cursor) >= Number(before)) throw new Error(I18n.t("历史分页游标未向前推进"));
    const page = {...result, start:result.older_cursor};
    if (!await renderPage(page, session, token)) return;
    pages.unshift(page); last++; trimCache();
  }
  async function navigate(select, message, ids = null) {
    if (busy || !pages.length) return;
    const session = S.cur, token = version;
    busy = true; error = ""; past = true; retryKind = ids ? "anchor" : "oldest"; retryIds = ids;
    controls(message);
    try {
      await select();
      if (!active(session, token)) return;
      if (ids) {
        const target = anchorRows().find(row => ids.includes(row.dataset.historyId));
        if (!target) throw new Error(I18n.t("当前记录中未显示这条消息"));
        if (!await jumpToAnchor(target)) throw new Error(I18n.t("当前记录中未显示这条消息"));
      }
      retryKind = ""; retryIds = null;
    } catch (e) {
      if (!active(session, token) || e.name === "AbortError") return;
      error = I18n.t`定位失败：${e.message}，点击重试`;
    } finally {
      if (active(session, token)) { busy = false; controls(); resumeQueue(); }
    }
  }
  async function latest({initial = false} = {}) {
    selectedAnchor = null; ++anchorJump; ignoreScroll = false;
    if (!initial && pages.length && renderedSession === S.cur) {
      traceStick = true; past = false; pending = false; controls(); traceScroll(true);
      if (!S.es) startStreams(false);
      return;
    }
    const session = S.cur, token = ++version;
    syncController?.abort(); syncing = false; syncController = null;
    stopStreams(); busy = true; error = ""; pending = false; controls();
    retryDirection = 0; retryKind = ""; retryIds = null;
    if (initial) {
      pages = []; first = 0; last = -1; past = false;
      if (viewport()) viewport().clear(); else box().replaceChildren();
      $("#events").replaceChildren();
      renderedSession = session;
      $("#um-rail")?.replaceChildren();
      anchorKey = null;
      ToolParameters.reset(session);
    }
    try {
      const result = await read();
      if (!active(session, token)) return;
      pages = [{...result, start:result.older_cursor}]; first = last = 0; past = false;
      await renderPage(pages[0], session, token, {mode:"replace", observe:initial});
      if (!active(session, token)) return;
      S.evSeen = result.cursor; busy = false; controls(); startStreams(false);
    } catch (e) {
      if (!active(session, token) || e.name === "AbortError") return;
      busy = false; ignoreScroll = false; error = I18n.t`读取失败：${e.message}`; controls();
      if (pages.length) startStreams(false);
    }
  }
  async function move(direction) {
    if (busy || !pages.length) return;
    if (direction > 0) {
      past = false; pending = false; controls();
      if (!S.es) startStreams(false);
      return;
    }
    if (!pages[0].has_more) return;
    const session = S.cur, token = version;
    busy = true; past = true; error = ""; controls();
    retryDirection = -1; retryKind = ""; retryIds = null;
    try { await prependPage(); }
    catch (e) {
      if (!active(session, token) || e.name === "AbortError") return;
      error = I18n.t`读取失败：${e.message}，继续滚动或点击重试`;
    } finally {
      if (active(session, token)) { busy = false; scrollTop = box().scrollTop; controls(); resumeQueue(); }
    }
  }
  async function oldest() {
    await navigate(async () => {
      const session = S.cur, token = version;
      while (pages[0].has_more && active(session, token)) await prependPage();
      if (!active(session, token)) return;
      const row = traceRows().find(row => !row.hidden && !row.classList?.contains("reader-filtered"));
      if (row) await jumpToAnchor(row);
    }, I18n.t("正在定位会话开头…"));
  }
  function onScroll() {
    const b = box(), direction = Math.sign(b.scrollTop - scrollTop);
    scrollTop = b.scrollTop;
    if (busy || ignoreScroll || S.view !== "session" || !direction) return;
    if (traceStick && !past) return;
    scrollDirection = direction;
    if (b.scrollHeight - b.scrollTop - b.clientHeight >= 24) past = true;
    if (scrollFrame !== null) return;
    scrollFrame = requestAnimationFrame(() => {
      scrollFrame = null;
      if (busy || ignoreScroll) return;
      selectedAnchor = null;
      markActiveAnchor(); hideAnchorPreview();
      if (scrollDirection < 0 && b.scrollTop < 180) move(-1);
      else if (scrollDirection > 0 && b.scrollHeight - b.scrollTop - b.clientHeight < 180) move(1);
    });
  }
  async function syncLatest() {
    if (syncing || busy || past || !pages.length || S.view !== "session" || S.es) return;
    const session = S.cur, token = version, seen = Number(S.evSeen) || 0;
    syncing = true; syncController = new AbortController();
    const signal = syncController.signal, batches = [];
    try {
      let before = null;
      while (true) {
        const result = await api(`/api/sessions/${session}/audit-page?activity=true&limit=${PAGE_SIZE}` +
          (before === null ? "" : `&before=${before}`), {signal});
        if (!active(session, token) || busy || past || S.es) return;
        batches.unshift(result.items.filter(item => item.id > seen));
        if (!result.has_more || result.older_cursor <= seen) break;
        const next = Number(result.older_cursor);
        if (!Number.isFinite(next) || next <= 0 || (before !== null && next >= before))
          throw new Error(I18n.t("历史分页游标未向前推进"));
        before = next;
      }
      for (const batch of batches) for (const item of batch) enqueue(item.event, item.id, session);
      await drain();
    } catch (e) {
      if (active(session, token) && e.name !== "AbortError") throw e;
    } finally {
      if (active(session, token)) { syncing = false; syncController = null; }
    }
  }
  function innerConsumesScroll(target, delta) {
    for (let el = target; el && el !== box(); el = el.parentElement) {
      if (el.nodeType !== 1 || el.scrollHeight <= el.clientHeight + 1) continue;
      const style = getComputedStyle(el);
      if (!/^(auto|scroll|overlay)$/.test(style.overflowY)) continue;
      if (delta < 0 ? el.scrollTop > 0 : el.scrollHeight - el.scrollTop - el.clientHeight > 1) return true;
      if (["contain", "none"].includes(style.overscrollBehaviorY)) return true;
    }
    return false;
  }
  box().addEventListener("scroll", onScroll, {passive:true});
  // 已停在边界再向界外滚是零位移（不会产生 scroll 事件），按滚轮意图直接翻页。
  // 向下尤其关键：任务会话的历史窗口可能整体聚合成一张折叠卡（高度小于视口、
  // 盒子不可滚动），没有这个逃生口用户会被困在该窗口里出不去。
  box().addEventListener("wheel", event => {
    if (S.view !== "session" || event.ctrlKey || innerConsumesScroll(event.target, event.deltaY)) return;
    // wheel 先于浏览器的滚动/动画帧；用户上滑立即撤销已排队的吸底。
    if(event.deltaY < 0) {
      traceStopFollowing();
      if(!busy && !ignoreScroll)past=true;
    }
    if (busy || ignoreScroll) return;
    const b = box();
    if (event.deltaY < 0) { if (b.scrollTop <= 0) move(-1); }
    else if (event.deltaY > 0 && b.scrollHeight - b.scrollTop - b.clientHeight <= 0) move(1);
  }, {passive:true});
  document.addEventListener("click", event => {
    const button = /** @type {HTMLButtonElement} */ (/** @type {HTMLElement} */ (event.target).closest("[data-history]"));
    if (!button || button.disabled || busy) return;
    if (button.dataset.history === "latest") latest();
    else if (button.dataset.history === "oldest") oldest();
    else if (button.dataset.history === "retry") {
      if (retryKind === "anchor" && retryIds?.length) locate(retryIds);
      else if (retryDirection) move(retryDirection);
      else latest({initial:!pages.length});
    }
  });
  return {open:() => latest({initial:true}), latest, syncLatest, enqueue, drain, clearLiveQueue, isPast:() => past,
    isRendering:() => busy || ignoreScroll,
    cancel() {
      viewport()?.stop();
      ++version; controller?.abort(); syncController?.abort(); anchorIndex?.controller?.abort(); clearLiveQueue();
      syncing = false; syncController = null;
      if (scrollFrame !== null) cancelAnimationFrame(scrollFrame);
      scrollFrame = null; pages = []; first = 0; last = -1; busy = false; past = false; ignoreScroll = false;
      anchorKey = null; anchorIndex = null; selectedAnchor = null; ++anchorJump; hideAnchorPreview();
      renderedSession = null;
      const rail = $("#um-rail");
      if (rail) { rail.replaceChildren(); rail.hidden = true; }
    }};
})();
