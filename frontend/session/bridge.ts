import { entryKind, matchesEntry, readingTitle } from './reader.ts';
import type { ReadingEntry } from './reader.ts';

export interface ReaderSnapshot {
  sessionId: string;
  entries: ReadingEntry[];
  appearance: 'light' | 'dark';
  language: string;
}

// 目录与筛选读取连续消息集合，不随虚拟列表挂载范围变化。
export function createReaderBridge() {
  const root = document.querySelector<HTMLElement>('#view-session')!;
  const trace = document.querySelector<HTMLElement>('#trace-box')!;
  const viewport = globalThis.window?.TraceViewport;
  const rows = () => viewport?.rows() || [...trace.querySelectorAll<HTMLElement>(':scope > .tl')];
  const media = matchMedia('(prefers-color-scheme: dark)');
  const listeners = new Set<() => void>();
  const nodes = new Map<string, HTMLElement>();
  const rowIds = new WeakMap<HTMLElement, string>();
  const entriesByRow = new WeakMap<HTMLElement, ReadingEntry>();
  let serial = 0;
  let snapshot: ReaderSnapshot = { sessionId: '', entries: [], appearance: 'light', language: 'zh-CN' };
  let timer: ReturnType<typeof setTimeout> | undefined;
  let query = '';
  let previousSession = '';
  let lastFocused: HTMLElement | undefined;
  const theme = () => {
    const explicit = document.documentElement.dataset.theme;
    return explicit === 'dark' || explicit !== 'light' && media.matches ? 'dark' as const : 'light' as const;
  };
  function resetSession() {
    const sessionId = root.dataset.sessionId || '';
    if (sessionId !== previousSession) {
      query = ''; previousSession = sessionId;
      lastFocused?.classList.remove('reader-target');
    }
    return sessionId;
  }
  function readEntry(row: HTMLElement): ReadingEntry {
    let entry = entriesByRow.get(row);
    if (entry) return entry;
    let id = rowIds.get(row);
    if (!id) { id = `reader-entry-${++serial}`; rowIds.set(row, id); }
    const heading = row.querySelector('.task-heading strong');
    const body = row.querySelector('.btxt, .task-stage-summary, .task-summary, .tl-say, .ang, .m');
    const contentHeading = body?.querySelector('h1, h2, h3');
    const actor = row.querySelector('.task-seal')?.getAttribute('aria-label') || row.querySelector('.who')?.textContent?.trim() || row.dataset.agent || '';
    const context = row.querySelector('.task-context')?.textContent?.trim() || '';
    const type = row.dataset.eventType || '';
    const text = row.textContent?.trim() || '';
    const kind = entryKind(type, row.dataset.taskPart || '', row.dataset.taskStatus || '', row.className);
    const english = document.documentElement.lang.startsWith('en');
    const fallback = english ? ({message:'Conversation',result:'Result',attention:'Needs attention',progress:'Task progress',activity:'Activity'})[kind]
      : ({message:'会话消息',result:'结果与交付',attention:'待关注事项',progress:'任务进展',activity:'运行记录'})[kind];
    // 身份放在辅助信息，标题只取正文；文件消息优先使用用户能辨认的文件名。
    const filename = row.querySelector('.fc-name')?.textContent?.trim() || '';
    entry = {
      id,
      title: type === 'report.ready' ? (english ? 'Report ready' : '解题报告已生成')
        : readingTitle(contentHeading?.textContent?.trim() || heading?.textContent?.trim() || (!body ? row.querySelector('.hd .k')?.textContent?.trim() : '') || '',
          body?.textContent?.trim() || filename, fallback),
      text, kind, actor: context || actor,
      time: row.querySelector('time')?.textContent || '',
    };
    entriesByRow.set(row, entry);
    return entry;
  }
  function read() {
    timer = undefined;
    const sessionId = resetSession();
    nodes.clear();
    const entries: ReadingEntry[] = [];
    for (const row of rows()) {
      if (row.hidden) continue;
      const entry = readEntry(row);
      nodes.set(entry.id, row);
      entries.push(entry);
    }
    snapshot = { sessionId, entries, appearance: theme(), language: document.documentElement.lang };
    listeners.forEach(listener => listener());
  }
  function schedule() { timer ??= setTimeout(read, 120); }
  function applyFilter(target: HTMLElement = trace) {
    resetSession();
    for (const row of target === trace ? rows() : target.querySelectorAll<HTMLElement>(':scope > .tl')) {
      row.classList.toggle('reader-filtered', !!query && !matchesEntry(readEntry(row), query));
    }
  }
  // 换窗提交前同步筛选，滚动锚点只能基于最终可见的布局恢复。
  const prepareRender = (event: Event) => applyFilter((event as CustomEvent<HTMLElement>).detail);
  trace.addEventListener('reader:render', prepareRender);
  const unsubscribe = viewport?.subscribe(changed => {
    for (const row of changed) entriesByRow.delete(row);
    applyFilter(); schedule();
  });
  const observer = new MutationObserver(records => {
    for (const record of records) {
      const element = record.target instanceof Element ? record.target : record.target.parentElement;
      const row = element?.closest<HTMLElement>('.tl');
      if (row) entriesByRow.delete(row);
    }
    // 新增正文和状态变化也必须在浏览器绘制前筛选；目录快照仍合并刷新。
    applyFilter();
    schedule();
  });
  if (!viewport) observer.observe(trace, { childList: true, characterData: true, subtree: true,
    attributes: true, attributeFilter: ['hidden', 'data-task-status', 'data-task-part', 'data-event-type'] });
  observer.observe(root, { attributes: true, attributeFilter: ['data-session-id'] });
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'lang'] });
  media.addEventListener('change', schedule);
  applyFilter();
  read();
  return {
    subscribe(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener); }; },
    getSnapshot: () => snapshot,
    filter(nextQuery: string) { query = nextQuery; applyFilter(); viewport?.refresh(); },
    navigate(id: string) {
      const row = nodes.get(id);
      if (!row || (!row.isConnected && !viewport?.has(row)) || snapshot.sessionId !== root.dataset.sessionId) return;
      trace.dispatchEvent(new Event('reader:navigate'));
      lastFocused?.classList.remove('reader-target');
      row.classList.add('reader-target'); lastFocused = row;
      if (viewport) { void viewport.navigate(row, 'center'); return; }
      row.scrollIntoView({ block: 'center', behavior: 'instant' });
      row.tabIndex = -1; row.focus({ preventScroll: true });
    },
    destroy() {
      trace.removeEventListener('reader:render', prepareRender);
      unsubscribe?.();
      observer.disconnect(); media.removeEventListener('change', schedule);
      if (timer !== undefined) clearTimeout(timer);
      for (const node of rows()) node.classList.remove('reader-filtered', 'reader-target');
      listeners.clear();
    },
  };
}
export type ReaderBridge = ReturnType<typeof createReaderBridge>;
