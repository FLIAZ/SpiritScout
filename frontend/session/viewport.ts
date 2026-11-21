import { Virtualizer, elementScroll, observeElementOffset, observeElementRect } from '@tanstack/virtual-core';

// 原生消息节点保留交互状态；只有视口附近的节点参与布局，滚动由一个内核负责。
export function createTraceViewport(trace: HTMLElement) {
  const initial = [...trace.querySelectorAll<HTMLElement>(':scope > .tl')];
  const canvas = document.createElement('div');
  canvas.className = 'trace-items';
  const sink = document.createElement('div');
  let rows: HTMLElement[] = [];
  let visible: HTMLElement[] = [];
  const membership = new Set<HTMLElement>();
  const ids = new WeakMap<HTMLElement, string>();
  const listeners = new Set<(changed: HTMLElement[]) => void>();
  const bottomPadding = 48;
  let readingPadding = bottomPadding;
  let serial = 0, generation = 0, navigation = 0, frame: number | undefined, rendering = false, renderAgain = false;
  const key = (row: HTMLElement) => {
    let id = ids.get(row);
    if (!id) {
      id = JSON.stringify([generation, row.dataset.historyId || `local-${++serial}`,
        row.dataset.agent || '', row.dataset.taskPart || row.dataset.eventType || '', row.className]);
      ids.set(row, id);
    }
    return id;
  };
  const observer = new MutationObserver(records => {
    const changed = new Set<HTMLElement>();
    for (const record of records) {
      const target = record.target instanceof Element ? record.target : record.target.parentElement;
      const row = target?.closest<HTMLElement>('.tl');
      if (row && membership.has(row)) changed.add(row);
    }
    if (changed.size) {
      listeners.forEach(listener => listener([...changed]));
      refresh();
    }
  });
  const virtualizer = new Virtualizer<HTMLElement, HTMLElement>({
    count: 0, getScrollElement: () => trace, estimateSize: () => 180,
    getItemKey: index => key(visible[index]!),
    observeElementRect, observeElementOffset, scrollToFn: elementScroll,
    anchorTo: 'end', followOnAppend: true, scrollEndThreshold: 24,
    overscan: 6, paddingStart: 24, paddingEnd: bottomPadding,
    onChange: () => render(),
  });
  // 已测量消息在视口上方的真实增长也要补偿，不能因用户正在上滑就推走阅读位置。
  virtualizer.shouldAdjustScrollPositionOnItemSizeChange = (item,_delta,instance) => {
    const offset = instance.scrollOffset ?? 0;
    return instance.itemSizeCache.has(item.key) ? item.end <= offset : item.start < offset;
  };
  function render() {
    if (rendering) { renderAgain = true; return; }
    rendering = true;
    try {
      canvas.style.height = `${virtualizer.getTotalSize()}px`;
      const items = virtualizer.getVirtualItems();
      const mounted = new Set(items.map(item => visible[item.index]!));
      for (const child of [...canvas.children]) if (!mounted.has(child as HTMLElement)) child.remove();
      for (const [position,item] of items.entries()) {
        const row = visible[item.index]!;
        row.dataset.index = String(item.index);
        row.style.transform = `translate(-50%, ${item.start}px)`;
        if (canvas.children[position] !== row) canvas.insertBefore(row,canvas.children[position] || null);
        virtualizer.measureElement(row);
      }
      canvas.style.height = `${virtualizer.getTotalSize()}px`;
      const next = virtualizer.getVirtualIndexes();
      if (next.length !== items.length || next[0] !== items[0]?.index || next.at(-1) !== items.at(-1)?.index) renderAgain = true;
    } finally { rendering = false; }
    if (renderAgain) { renderAgain = false; queueMicrotask(render); }
  }
  function refresh() {
    const next = rows.filter(row => !row.hidden && !row.classList.contains('reader-filtered'));
    for (const row of next) {
      const conversation = row.matches('.tl-conv,.tl-task-step,.tl-turn');
      row.toggleAttribute('data-reader-separated', conversation);
    }
    visible = next;
    const keys = next.map(key);
    // 每一版 key 提取器关闭在该版数组上，内核才能区分 prepend 与 append。
    virtualizer.setOptions({ ...virtualizer.options, count: next.length, paddingEnd: readingPadding,
      getItemKey: index => keys[index]! });
    render();
    virtualizer._willUpdate();
    render();
  }
  function observe(row: HTMLElement) {
    observer.observe(row, { childList: true, subtree: true, characterData: true,
      attributes: true, attributeFilter: ['hidden', 'data-task-status', 'data-task-part', 'data-event-type'] });
  }
  function commit(incoming: HTMLElement[], mode: 'replace' | 'prepend' | 'append') {
    if (mode === 'replace') {
      clear();
    } else if (mode === 'prepend' && visible.length) {
      // 短历史窗口的底部留白本就可见。补页后也要能滚到原来的阅读锚点。
      const missing = trace.scrollTop + trace.clientHeight - virtualizer.getTotalSize();
      readingPadding += Math.max(0, Math.ceil(missing));
    }
    for (const row of incoming) { key(row); membership.add(row); observe(row); }
    rows = mode === 'prepend' ? [...incoming, ...rows] : [...rows, ...incoming];
    listeners.forEach(listener => listener(incoming));
    refresh();
  }
  function append(row: HTMLElement) {
    membership.add(row); observe(row); rows.push(row);
    if (frame !== undefined) return;
    frame = requestAnimationFrame(() => { frame = undefined; listeners.forEach(listener => listener([row])); refresh(); });
  }
  function clear() {
    if (frame !== undefined) cancelAnimationFrame(frame);
    frame = undefined; generation++; observer.disconnect(); readingPadding = bottomPadding;
    rows = []; visible = []; membership.clear(); sink.replaceChildren();
    ++navigation; virtualizer.cancelScroll();
    // 同步撤销旧 count/key 后才能清测量；measure 会立即触发 onChange。
    virtualizer.setOptions({ ...virtualizer.options, count: 0, getItemKey: () => '' });
    virtualizer.measure();
    refresh(); listeners.forEach(listener => listener([]));
  }
  const cleanup = virtualizer._didMount();
  trace.classList.add('trace-virtual'); trace.replaceChildren(canvas);
  virtualizer._willUpdate();
  if (initial.length) commit(initial, 'replace');
  return {
    sink, rows: () => rows, mounted: () => [...canvas.children] as HTMLElement[],
    has: (row: HTMLElement | undefined | null) => !!row && membership.has(row),
    append, commit, clear, refresh,
    subscribe(listener: (changed: HTMLElement[]) => void) { listeners.add(listener); return () => { listeners.delete(listener); }; },
    toEnd() { readingPadding = bottomPadding; refresh(); virtualizer.scrollToEnd({ behavior: 'instant' }); },
    stop() { ++navigation; virtualizer.cancelScroll(); },
    isAtEnd: () => virtualizer.isAtEnd(),
    async navigate(row: HTMLElement, align: 'start' | 'center' = 'start') {
      const request = ++navigation;
      refresh(); const index = visible.indexOf(row);
      if (index < 0) return false;
      virtualizer.scrollToIndex(index, { align, behavior: 'instant' });
      await new Promise(requestAnimationFrame);
      if (request !== navigation || !membership.has(row)) return false;
      render(); row.tabIndex = -1; row.focus({ preventScroll: true });
      return row.isConnected;
    },
    destroy() { clear(); cleanup(); listeners.clear(); },
  };
}

export type TraceViewport = ReturnType<typeof createTraceViewport>;
declare global { interface Window { TraceViewport?: TraceViewport } }
