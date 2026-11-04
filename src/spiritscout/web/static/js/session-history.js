"use strict";
/* 三段 DOM 窗口与有界页面缓存共用字节游标；用户消息目录独立保留。 */
const SessionHistory = (() => {
  const WINDOW = 3, PAGE_SIZE = 160, CACHE_PAGES = 12;
  let version = 0, pages = [], first = 0, last = -1, busy = false, past = false;
  let queue = [], head = 0, frame = null, waiters = [], controller = null;
  let scrollFrame = null, ignoreScroll = false, pending = false, error = "", retryDirection = -1;
  let scrollTop = 0, scrollDirection = 0, cacheClock = 0, retryKind = "", retryIds = null;
  let syncing = false, syncController = null;
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
    const oldestButton = root.querySelector('[data-history="oldest"]');
    if (oldestButton) oldestButton.disabled = busy || !pages.length || (!pages[0].has_more && first === 0);
    const retry = root.querySelector('[data-history="retry"]');
    if (retry) { retry.hidden = !error; retry.disabled = busy; }
    refreshAnchors();
  }
  // 用户消息目录独立于滚动窗口；同次发送只按消息信封 ID 分组。
  let anchorKey = null, anchorIndex = null, previewBar = null;
  const anchorRows = () => [...box().children].filter(el => el.classList.contains("tl-user") && el.dataset.historyId);
  const messageKey = (detail, id) => detail?.message_id
    ? JSON.stringify(["message", String(detail.message_id)]) : JSON.stringify(["event", String(id)]);
  function anchorText(row) {
    const text = row.querySelector(".btxt")?.textContent.trim();
    if (text) return text;
    const name = row.querySelector(".fc-name")?.textContent.trim();
    return name ? `[附件] ${name}` : "用户消息";
  }
  function groupsFromEvents(items) {
    const groups = [], byKey = new Map();
    for (const item of [...items].sort((a, b) => Number(a.id) - Number(b.id))) {
      const kind = item.event?.type, d = item.event?.detail || {};
      if (!["user.message", "user.attachment"].includes(kind)) continue;
      const key = messageKey(d, item.id);
      let group = byKey.get(key);
      if (!group) { group = {key, ids: [], parts: []}; byKey.set(key, group); groups.push(group); }
      group.ids.push(String(item.id));
      group.parts.push(kind === "user.message" ? String(d.content || "").trim() : `[附件] ${d.name || "附件"}`);
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
    if (!state || !item.id || !["user.message", "user.attachment"].includes(item.event?.type)) return;
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
        const result = await api(`/api/sessions/${state.session}/audit-page?types=user.message,user.attachment&limit=250` +
          (before === null ? "" : `&before=${before}`), {signal: state.controller.signal});
        if (anchorIndex !== state || S.cur !== state.session) return;
        for (const item of result.items) recordAnchor(item, state);
        const next = Number(result.older_cursor);
        if (result.has_more && (!Number.isFinite(next) || next <= 0 || (before !== null && next >= before)))
          throw new Error("存档点分页游标未向前推进");
        state.before = next;
        if (!result.has_more) { state.complete = true; break; }
        before = next;
        refreshAnchors();
      }
    } catch (e) {
      if (anchorIndex === state && e.name !== "AbortError") state.error = e.message || "存档点读取失败";
    } finally {
      if (anchorIndex === state) { state.loading = false; refreshAnchors(); }
    }
  }
  function refreshAnchors() {
    const root = $("#um-rail"), state = indexForSession();
    if (!root || !state) return;
    // 兼容由旧渲染入口产生的行；完整事件目录优先，不用 DOM 文本覆盖原文。
    for (const row of anchorRows()) if (!state.events.has(row.dataset.historyId)) {
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
            if (target) jumpToAnchor(target); else await locate(ids);
          });
          el.addEventListener("mouseenter", () => showAnchorPreview(el));
          el.addEventListener("mouseleave", hideAnchorPreview);
        }
        el.disabled = busy; el.dataset.historyId = group.ids[0]; el.dataset.ids = group.ids.join("|");
        const text = group.parts.filter(Boolean).join("\n").slice(0, 400) || "用户消息";
        el.dataset.preview = text; el.setAttribute("aria-label", `回到这条消息：${text.slice(0, 60)}`);
        return el;
      });
      if (state.error || (state.loading && !bars.length)) {
        const button = document.createElement("button"); button.type = "button"; button.className = "btn sm";
        button.textContent = state.loading ? "…" : "↻"; button.disabled = state.loading;
        button.title = state.error ? `存档点未补全：${state.error}，点击重试` : "正在补全会话存档点";
        button.setAttribute("aria-label", state.error ? "重试加载会话存档点" : "正在补全会话存档点");
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
    const boundary = box().getBoundingClientRect().top + 13;
    const rows = [...box().children].filter(row => visibleRect(row));
    const row = rows.filter(row => row.getBoundingClientRect().top <= boundary).at(-1) || rows[0];
    let ref = Number(row?.dataset.historyStart || row?.dataset.historyId);
    if (!Number.isFinite(ref)) return;
    if (row.classList.contains("tl-user") && row.getBoundingClientRect().top > boundary) ref--;
    let key = null;
    for (const group of state.groups) {
      if (Number(group.ids[0]) > ref) break;
      key = group.key;
    }
    for (const bar of root.children) if (bar.dataset.anchorGroup)
      bar.classList.toggle("active", bar.dataset.anchorGroup === key);
  }
  function jumpToAnchor(row) {
    ignoreScroll = true;
    box().scrollTop += row.getBoundingClientRect().top - box().getBoundingClientRect().top - 12;
    traceStick = false; past = true; controls();
    paint().then(() => { ignoreScroll = false; scrollTop = box().scrollTop; });
  }
  async function locate(ids) {
    await navigate(async () => {
      const numbers = ids.map(Number).filter(Number.isFinite);
      if (!numbers.length) throw new Error("消息游标无效");
      // 新消息可能在回看期间到达：补取最新页后仍保持回看，不偷换为跟随模式。
      if (Math.max(...numbers) > pages.at(-1).cursor) {
        const result = await read();
        pages = [{...result, start: result.older_cursor}]; first = last = 0;
      }
      let index = pageForIds(ids);
      while (index < 0 && pages[0].has_more) { await prependPage(); index = pageForIds(ids); }
      if (index < 0) throw new Error("未找到这条消息，请刷新存档点后重试");
      return index;
    }, "正在定位消息…", ids);
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
        // 保留邻近已读页；更远的原始事件由有界缓存按字节游标重读。
        // 仅跟随最新时滑动窗口；历史阅读期间不删除用户正在看的内容。
        busy = true;
        const session = S.cur, token = version;
        const b = box(), gap = b.scrollHeight - b.scrollTop - b.clientHeight;
        // 直播轮换重绘：贴底跟随才吸底；用户已上滑阅读时锚定当前可视位置，轮换不甩底。
        const row = gap < 24 ? null : position();
        const fine = gap < 24 ? null : finePosition();
        redraw(session, token, row || fine ? {anchor: {...(row || {}), ...(fine ? {fine} : {})}} : {}).finally(() => {
          if (!active(session, token)) return;
          busy = false; trimCache(); controls();
          if (head < queue.length && frame === null) frame = requestAnimationFrame(flush);
        });
        head--; return;
      }
      remember(page, item);
      const previous = box().lastElementChild;
      renderAuditEvent(item.event, {eventId: item.id});
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
    // 回看和重连清队列都不能丢掉新用户消息的存档点。
    if (session === S.cur && S.view === "session") recordAnchor({event, id});
    queue.push({event, id, session});
    if (frame === null) frame = requestAnimationFrame(flush);
  }
  function drain() {
    return head < queue.length ? new Promise(resolve => waiters.push(resolve)) : Promise.resolve();
  }
  function identity(row) {
    // 「本页回合片段」行的身份随窗口滑动而变（首行 id 换页即换），
    // 按演员稳定标识，否则锚点失配会跳顶并引发连环翻页。
    if (row.classList?.contains("tl-frag")) return JSON.stringify(["frag", row.dataset.agent || ""]);
    if (row.dataset.taskId) return JSON.stringify([row.dataset.agent, row.dataset.taskId, row.dataset.stepKey || row.dataset.taskPart || "task"]);
    return JSON.stringify([row.dataset.agent, row.dataset.taskId, row.dataset.eventTs, row.dataset.historyId]);
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
  function position({fromEnd = false} = {}) {
    const rect = box().getBoundingClientRect(), rows = [...box().children];
    if (fromEnd) rows.reverse();
    for (const row of rows) {
      const hit = visibleRect(row, rect);
      if (hit) return {key: identity(row), offset: hit.top - rect.top,
        actor: row.dataset.agent, task: row.dataset.taskId, ref: Number(row.dataset.historyId)};
    }
    return null;
  }
  const FINE_SEL = "[data-step-key],[data-tool-key],[data-route]";
  function fineIdentity(el) {
    const d = el.dataset || {};
    if (!(d.stepKey || d.toolKey || d.route)) return null;
    const row = el.closest?.(".tl[data-task-id]") || el.closest?.("[data-task-id]");
    const root = row?.dataset || {};
    const kind = d.stepKey ? "step" : d.toolKey ? "tool" : "route";
    // 阶段卡与其执行明细共用 stepKey；角色和结构身份也必须纳入，防止误钉另一节点。
    const role = ["task-step", "task-cmd", "task-stage", "task-cmds"].find(name => el.classList?.contains(name)) || el.tagName || "";
    const base = JSON.stringify([kind, root.agent || "", root.taskId || "", role, d.stepKey || d.toolKey || d.route]);
    return {key: JSON.stringify([base, root.taskPart || ""]), base, ref: Number(root.historyId)};
  }
  function finePosition({fromEnd = false} = {}) {
    const rect = box().getBoundingClientRect(), els = [...box().querySelectorAll(FINE_SEL)];
    if (fromEnd) els.reverse();
    for (const el of els) {
      const id = fineIdentity(el), hit = id && visibleRect(el, rect);
      if (hit) return {...id, offset: hit.top - rect.top};
    }
    return null;
  }
  function findFine(anchor) {
    if (!anchor) return null;
    const candidates = [...box().querySelectorAll(FINE_SEL)].filter(el => visibleRect(el));
    const exact = candidates.find(el => fineIdentity(el)?.key === anchor.key);
    if (exact) return exact;
    // 窗口补入旧消息时，首段的边界身份可能补全；用同阶段与事件范围找回原段。
    const matches = candidates.filter(el => fineIdentity(el)?.base === anchor.base);
    return matches.find(el => containsRef(el.closest?.(".tl[data-task-id]"), anchor.ref)) || (matches.length === 1 ? matches[0] : null);
  }
  function containsRef(row, ref) {
    const d = row?.dataset || {}, start = Number(d.historyStart || d.historyId), end = Number(d.historyEnd || d.historyId);
    return Number.isFinite(ref) && ref > 0 && start <= ref && ref <= end;
  }
  const TEXT_SEL = ".task-stage-summary,.task-summary,.btxt,.tl-say";
  function textNodes(root) {
    if (!document.createTreeWalker) return [];
    const walker = document.createTreeWalker(root, 4), nodes = [];
    while (walker.nextNode()) if (walker.currentNode.textContent.trim()) nodes.push(walker.currentNode);
    return nodes;
  }
  function textPosition() {
    if (!document.createRange || !document.createTreeWalker) return null;
    const viewport = box().getBoundingClientRect();
    for (const block of box().querySelectorAll(TEXT_SEL)) {
      if (!visibleRect(block, viewport)) continue;
      const row = block.closest(".tl"), range = document.createRange();
      const nodes = textNodes(block);
      for (const [index, node] of nodes.entries()) {
        // 文本块可能跨页拼接，锚定可见字符而非整段高度或随页变化的段编号。
        for (let start = 0; start < node.length; start += 64) {
          const end = Math.min(start + 64, node.length);
          range.setStart(node, start); range.setEnd(node, end);
          const rect = range.getBoundingClientRect();
          if (rect.bottom <= viewport.top + 12 || rect.top >= viewport.bottom || !rect.width) continue;
          const quote = node.textContent.slice(start, end);
          if (!quote.trim()) continue;
          return {quote, prefix: node.textContent.slice(Math.max(0, start - 64), start),
            suffix: node.textContent.slice(end, end + 64), offset: rect.top - viewport.top,
            previous: nodes[index - 1]?.textContent.slice(-192) || "", next: nodes[index + 1]?.textContent.slice(0, 192) || "",
            actor: row?.dataset.agent, task: row?.dataset.taskId, ref: Number(row?.dataset.historyId)};
        }
      }
    }
    return null;
  }
  function findText(anchor) {
    if (!anchor) return null;
    const hits = [];
    for (const block of box().querySelectorAll(TEXT_SEL)) {
      const row = block.closest(".tl");
      if (row?.dataset.agent !== anchor.actor || row?.dataset.taskId !== anchor.task || !visibleRect(block)) continue;
      const nodes = textNodes(block);
      for (const [index, node] of nodes.entries()) {
        let start = node.textContent.indexOf(anchor.quote);
        while (start >= 0) {
          const end = start + anchor.quote.length;
          const score = Number(node.textContent.slice(Math.max(0, start - anchor.prefix.length), start) === anchor.prefix)
            + Number(node.textContent.slice(end, end + anchor.suffix.length) === anchor.suffix)
            + Number(containsRef(row, anchor.ref))
            + 2 * Number(!!anchor.previous && nodes[index - 1]?.textContent.slice(-192) === anchor.previous)
            + 2 * Number(!!anchor.next && nodes[index + 1]?.textContent.slice(0, 192) === anchor.next);
          hits.push({node, start, end, score});
          start = node.textContent.indexOf(anchor.quote, start + 1);
        }
      }
    }
    hits.sort((a, b) => b.score - a.score);
    if (!hits.length) return null;
    const hit = hits[0], range = document.createRange();
    range.setStart(hit.node, hit.start); range.setEnd(hit.node, hit.end);
    return range;
  }
  function capturePosition(fromEnd = false) {
    return {...(position({fromEnd}) || {}), fine: finePosition({fromEnd}), text: textPosition()};
  }
  async function redraw(session, token, {anchor = null, follow = true, observe = false, preserve = false, fromEnd = false} = {}) {
    const opened = new Set(), closed = new Set(), tools = new Map(), details = [];
    const toolKey = el => el.dataset.stepKey || el.dataset.toolKey || el.dataset.id;
    const detailKey = (row, el, i) => `${identity(row)}:${toolKey(el.closest?.('.tl-act') || el) || i}:${el.className}`;
    const scope = row => JSON.stringify([row.dataset.agent, row.dataset.taskId]);
    for (const row of box().children) {
      row.querySelectorAll("details").forEach((el, i) => {
        (el.open ? opened : closed).add(detailKey(row, el, i));
        details.push({scope:scope(row), ref:Number(row.dataset.historyId), key:toolKey(el.closest?.(".tl-act") || el),
          role:el.className, open:el.open});
      });
      row.querySelectorAll(".tl-act").forEach(el => {
        const output = el.querySelector(".tl-output");
        tools.set(`${scope(row)}:${toolKey(el)}`, {open:el.classList.contains("open"), top:output?.scrollTop || 0, left:output?.scrollLeft || 0});
      });
    }
    ignoreScroll = true;
    const b = box(), wasTop = b.scrollTop, wasMax = b.scrollHeight - b.clientHeight;
    const overflowAnchor = b.style?.overflowAnchor;
    // 直播保留原生锚定；原子换窗期间暂时关闭，避免它与手工补偿叠加。
    if (b.style) b.style.overflowAnchor = "none";
    const stage = typeof traceBeginRender === "function" ? traceBeginRender() : null;
    const targetBox = stage || b;
    let complete = false;
    try {
      try {
        traceReset({preservePanels: !observe}); traceStick = false;
        let deadline = performance.now() + 6;
        for (let i = first; i <= last; i++) for (const item of pages[i].items || []) {
          const previous = targetBox.lastElementChild;
          renderAuditEvent(item.event, {observe, eventId: item.id});
          const row = targetBox.lastElementChild;
          if (row && row !== previous && !row.dataset.historyId) row.dataset.historyId = String(item.id);
          if (performance.now() >= deadline) {
            await paint();
            if (!active(session, token)) return;
            deadline = performance.now() + 6;
          }
        }
        if (typeof TaskTrace !== "undefined") TaskTrace.flush();
        complete = true;
      } finally {
        if (stage) {
          if (complete && active(session, token) && preserve) anchor = capturePosition(fromEnd);
          traceEndRender(stage, complete && active(session, token));
        }
      }
      for (const row of b.children) {
        row.querySelectorAll("details").forEach((el, i) => {
          if (opened.has(detailKey(row, el, i))) el.open = true;
          else if (closed.has(detailKey(row, el, i))) el.open = false;
          else {
            const tool = el.closest?.(".tl-act"), key = toolKey(tool || el);
            const saved = details.find(item => item.scope === scope(row) && item.role === el.className &&
              (tool ? key && key === item.key : containsRef(row, item.ref)));
            if (saved) el.open = saved.open;
            else if (!follow && el.classList.contains("task-stages")) el.open = true;
          }
        });
        row.querySelectorAll(".tl-act").forEach(el => {
          const saved = tools.get(`${scope(row)}:${toolKey(el)}`);
          if (!saved) return;
          el.classList.toggle("open", saved.open); el.querySelector("button.hd").setAttribute("aria-expanded", String(saved.open));
          const output = el.querySelector(".tl-output");
          if (output) { output.scrollTop = saved.top; output.scrollLeft = saved.left; }
        });
      }
      ToolParameters.prune?.();
      const textRange = findText(anchor?.text), fineEl = findFine(anchor?.fine);
      const rowEl = anchor?.key && ([...b.children].find(el => identity(el) === anchor.key) ||
        [...b.children].find(el => anchor.task && el.dataset.agent === anchor.actor && el.dataset.taskId === anchor.task && containsRef(el, anchor.ref)));
      const target = textRange || fineEl || rowEl, offset = textRange ? anchor.text.offset : fineEl ? anchor.fine.offset : anchor?.offset;
      traceStick = false;
      if (target && offset != null) b.scrollTop += target.getBoundingClientRect().top - b.getBoundingClientRect().top - offset;
      else if (follow) { b.scrollTop = b.scrollHeight; traceStick = true; }
      else if (anchor) {
        const max = b.scrollHeight - b.clientHeight;
        b.scrollTop = wasMax > 0 && max > 0 ? max * (wasTop / wasMax) : 0;
      } else b.scrollTop = 0;
      await paint();
      if (active(session, token)) traceScroll();
    } finally {
      if (b.style) b.style.overflowAnchor = overflowAnchor || "";
      if (active(session, token)) { scrollTop = b.scrollTop; ignoreScroll = false; }
    }
  }
  async function read(before = null) {
    const session = S.cur, token = version;
    controller?.abort(); controller = new AbortController();
    const result = await api(`/api/sessions/${session}/audit-page?activity=true&limit=${PAGE_SIZE}` +
      (before === null ? "" : `&before=${before}`), {signal: controller.signal});
    if (!active(session, token)) { const e = new Error("会话已切换"); e.name = "AbortError"; throw e; }
    for (const item of result.items) recordAnchor(item);
    result.touched = ++cacheClock;
    return result;
  }
  function trimCache() {
    const cached = pages.map((page, i) => ({page, i})).filter(({page}) => page.items)
      .sort((a, b) => (b.page.touched || 0) - (a.page.touched || 0));
    let keep = Math.max(CACHE_PAGES - (last - first + 1), 0);
    for (const {page, i} of cached) {
      if (i >= first && i <= last) continue;
      if (keep > 0) keep--; else page.items = null;
    }
  }
  function pageForIds(ids) {
    return pages.findIndex(page => ids.some(id => Number(id) > page.start && Number(id) <= page.cursor));
  }
  async function prependPage() {
    const before = pages[0].older_cursor, result = await read(before);
    if (Number(result.older_cursor) >= Number(before)) throw new Error("历史分页游标未向前推进");
    pages.unshift({...result, start: result.older_cursor}); first++; last++;
  }
  async function fillWindow() {
    for (let i = first; i <= last; i++) {
      const page = pages[i];
      if (!page.items) {
        const result = await read(page.cursor);
        page.items = result.items.filter(item => item.id > page.start && item.id <= page.cursor);
      }
      page.touched = ++cacheClock;
    }
  }
  async function navigate(select, message, ids = null) {
    if (busy || !pages.length) return;
    const session = S.cur, token = version, old = {pages: pages.slice(), first, last, past};
    let committed = false;
    busy = true; error = ""; past = true; retryKind = ids ? "anchor" : "oldest"; retryIds = ids;
    controls(message);
    try {
      const index = await select();
      if (!active(session, token)) return;
      first = Math.max(0, Math.min(index - (ids ? 1 : 0), pages.length - WINDOW));
      last = Math.min(pages.length - 1, first + WINDOW - 1);
      await fillWindow();
      await redraw(session, token, {follow: false});
      if (!active(session, token)) return;
      committed = true;
      busy = false; trimCache(); controls();
      if (ids) {
        const target = anchorRows().find(row => ids.includes(row.dataset.historyId));
        if (!target) throw new Error("当前记录中未显示这条消息");
        jumpToAnchor(target);
      }
      retryKind = ""; retryIds = null;
    } catch (e) {
      if (!active(session, token) || e.name === "AbortError") return;
      if (!committed) { pages = old.pages; first = old.first; last = old.last; past = old.past || pending; }
      busy = false; ignoreScroll = false; scrollTop = box().scrollTop;
      error = `定位失败：${e.message}，点击重试`; controls();
    }
  }
  async function latest({initial = false} = {}) {
    const session = S.cur, token = ++version;
    syncController?.abort(); syncing = false; syncController = null;
    stopStreams(); busy = true; error = ""; pending = false; controls();
    retryDirection = 0; retryKind = ""; retryIds = null;
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
    const session = S.cur, token = version;
    // 向旧端翻页时，视口顶部正是新旧内容的缝合处（本页回合片段会变高、换头），
    // 行级锚点取视口底部一行；有带稳定键的细粒度节点（阶段卡/命令/工具行）时优先细锚。
    const anchor = capturePosition(direction < 0);
    const old = {first,last,past};
    let index = direction < 0 ? first - 1 : last + 1;
    if (index < 0 && !pages[0].has_more) return;
    if (index >= pages.length) {
      if (past) { past = false; pending = false; controls(); startStreams(false); }
      return;
    }
    busy = true; past = true; error = ""; controls();
    retryDirection = direction; retryKind = ""; retryIds = null;
    try {
      let page = index < 0 ? null : pages[index];
      if (!page?.items) {
        const result = await read(page ? page.cursor : pages[0].older_cursor);
        if (!active(session, token)) return;
        if (page) page.items = result.items.filter(item => item.id > page.start);
        else { page = {...result, start:result.older_cursor}; }
      }
      if (!active(session, token)) return;
      page.touched = ++cacheClock;
      if (index < 0) { pages.unshift(page); first++; last++; index = 0; }
      if (direction < 0) { first = index; last = Math.min(last, first + WINDOW - 1); }
      else { last = index; first = Math.max(first, last - WINDOW + 1); }
      await redraw(session, token, {anchor, follow:false, preserve:true, fromEnd:direction < 0});
      if (!active(session, token)) return;
      const atEnd = last === pages.length - 1 && box().scrollHeight - box().scrollTop - box().clientHeight < 180;
      past = !atEnd;
      if (atEnd) pending = false;
      busy = false; trimCache(); controls();
      if (last === pages.length - 1 && !past) startStreams(false);
    } catch (e) {
      if (!active(session, token) || e.name === "AbortError") return;
      first = old.first; last = old.last; past = old.past || pending;
      busy = false; ignoreScroll = false; error = `读取失败：${e.message}，继续滚动或点击重试`; controls();
    }
  }
  // 回到初始消息：沿 older_cursor 逐页向旧端补读直到会话开头，一次重绘定位到第一条记录。
  async function oldest() {
    if (busy || !pages.length) return;
    const session = S.cur, token = version;
    busy = true; error = ""; past = true; retryDirection = -1; retryKind = ""; retryIds = null;
    controls("正在定位会话开头…");
    const old = {first, last};
    let added = 0;
    try {
      while (pages[0].has_more && active(session, token)) {
        const result = await read(pages[0].older_cursor);
        if (!active(session, token)) return;
        pages.unshift({...result, start: result.older_cursor});
        added++; first++; last++; trimCache();
      }
      first = 0; last = Math.min(pages.length - 1, WINDOW - 1);
      await fillWindow();
      await redraw(session, token, {follow: false});
      if (!active(session, token)) return;
      pending = past && pending; busy = false; trimCache(); controls();
    } catch (e) {
      if (!active(session, token) || e.name === "AbortError") return;
      first = old.first + added; last = old.last + added;
      busy = false; ignoreScroll = false; error = `读取失败：${e.message}，继续滚动或点击重试`; controls();
    }
  }
  function onScroll() {
    const b = box(), direction = Math.sign(b.scrollTop - scrollTop);
    scrollTop = b.scrollTop;
    if (busy || ignoreScroll || S.view !== "session" || !direction) return;
    scrollDirection = direction;
    if (b.scrollHeight - b.scrollTop - b.clientHeight >= 24) past = true;
    if (scrollFrame !== null) return;
    scrollFrame = requestAnimationFrame(() => {
      scrollFrame = null;
      if (busy || ignoreScroll) return;
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
          throw new Error("历史分页游标未向前推进");
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
    if (busy || ignoreScroll || S.view !== "session" || event.ctrlKey || innerConsumesScroll(event.target, event.deltaY)) return;
    const b = box();
    if (event.deltaY < 0) { if (b.scrollTop <= 0) move(-1); }
    else if (event.deltaY > 0 && b.scrollHeight - b.scrollTop - b.clientHeight <= 0) move(1);
  }, {passive:true});
  document.addEventListener("click", event => {
    const button = event.target.closest("[data-history]");
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
      ++version; controller?.abort(); syncController?.abort(); anchorIndex?.controller?.abort(); clearLiveQueue();
      syncing = false; syncController = null;
      if (scrollFrame !== null) cancelAnimationFrame(scrollFrame);
      scrollFrame = null; pages = []; first = 0; last = -1; busy = false; past = false; ignoreScroll = false;
      anchorKey = null; anchorIndex = null; hideAnchorPreview();
    }};
})();
