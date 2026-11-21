import { useEffect, useState, useSyncExternalStore } from 'react';
import { createRoot } from 'react-dom/client';
import { Button, IconButton, Popover, Text, TextField, Theme } from '@radix-ui/themes';
import '@radix-ui/themes/tokens/base.css';
import '@radix-ui/themes/tokens/colors/gray.css';
import '@radix-ui/themes/tokens/colors/sand.css';
import '@radix-ui/themes/tokens/colors/bronze.css';
import '@radix-ui/themes/components.css';
import { createReaderBridge } from './bridge';
import type { ReaderBridge } from './bridge';
import { matchesEntry, outlineSections } from './reader';
import { createTraceViewport } from './viewport';
import './session.css';
import '../workspace.css';

function SearchIcon() {
  return <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/></svg>;
}

function SessionReader({ bridge }: { bridge: ReaderBridge }) {
  const state = useSyncExternalStore(bridge.subscribe, bridge.getSnapshot);
  const [preferences, setPreferences] = useState({ sessionId: state.sessionId, query: '' });
  const current = preferences.sessionId === state.sessionId ? preferences : { sessionId: state.sessionId, query: '' };
  const { query } = current;
  const [outlineOpen, setOutlineOpen] = useState(false);
  const english = state.language.startsWith('en');
  const t = (zh: string, en: string) => english ? en : zh;
  const sections = { message: t('对话与要求', 'Conversation and requests'), result: t('结果与交付', 'Results and deliverables'), attention: t('需要关注', 'Needs attention'), progress: t('任务进展', 'Task progress'), activity: t('运行记录', 'Activity') };
  const visible = state.entries.filter(entry => matchesEntry(entry, query));
  useEffect(() => { bridge.filter(query); }, [bridge, state.sessionId, query]);
  useEffect(() => { setOutlineOpen(false); }, [state.sessionId]);
  function navigate(id: string) { setOutlineOpen(false); bridge.navigate(id); }
  return <Theme appearance={state.appearance} accentColor="bronze" grayColor="sand" radius="medium" scaling="100%" className="session-reader-theme">
    <div className="reader-toolbar" aria-label={t('会话阅读工具', 'Conversation reading tools')}>
      <div className="reader-leading">
        <Text size="1" color="gray" className="reader-count" role="status">{query ? `${visible.length} / ${state.entries.length}` : t(`${state.entries.length} 条记录`, `${state.entries.length} entries`)}</Text>
      </div>
      <div className="reader-controls">
        <TextField.Root size="1" value={query} type="search" placeholder={t('查找当前记录', 'Find in loaded history')} aria-label={t('查找已加载的会话记录', 'Search loaded conversation history')} onChange={event => setPreferences({ ...current, query: event.target.value })}>
          <TextField.Slot><SearchIcon /></TextField.Slot>
          {query && <TextField.Slot><IconButton size="1" variant="ghost" aria-label={t('清除搜索', 'Clear search')} onClick={() => setPreferences({ ...current, query: '' })}>×</IconButton></TextField.Slot>}
        </TextField.Root>
        <Popover.Root open={outlineOpen} onOpenChange={setOutlineOpen}>
          <Popover.Trigger><Button size="1" variant="ghost" className="reader-outline-trigger">{t('目录', 'Outline')}<span aria-hidden="true">☷</span></Button></Popover.Trigger>
          <Popover.Content className="reader-outline" size="2" align="end" style={{ width: 420, maxWidth: 'calc(100vw - 32px)' }}>
            <Text as="p" size="2" weight="medium">{t('阅读目录', 'Reading outline')}</Text>
            <p className="reader-outline-note">{t('按内容分类，仅索引已加载记录。', 'Grouped by content; loaded history only.')}</p>
            <nav className="reader-outline-list" aria-label={t('会话内容目录', 'Conversation outline')}>
              {outlineSections(visible).map(section => <section key={section.kind} className="reader-outline-section">
                <h3>{sections[section.kind]}<span>{section.entries.length}</span></h3>
                {section.entries.map(entry => <button key={entry.id} type="button" title={entry.title} onClick={() => navigate(entry.id)}>
                  <span className={`reader-entry-dot ${entry.kind}`} aria-hidden="true"/>
                  <span className="reader-outline-copy"><span>{entry.title}</span><small>{entry.actor}</small></span>
                  <time>{entry.time}</time>
                </button>)}
              </section>)}
              {!visible.length && <Text as="p" size="2" color="gray">{t('没有匹配的记录', 'No matching entries')}</Text>}
            </nav>
          </Popover.Content>
        </Popover.Root>
      </div>
    </div>
    {query && !visible.length && <div className="reader-empty" role="status">
      {t('当前已加载的记录中没有匹配内容。', 'No matches in the currently loaded history.')}
      <button type="button" onClick={() => setPreferences({ ...current, query: '' })}>{t('显示全部记录', 'Show all entries')}</button>
    </div>}
  </Theme>;
}

const host = document.querySelector('#session-reader');
if (host) {
  const trace = document.querySelector<HTMLElement>('#trace-box');
  if (trace) window.TraceViewport = createTraceViewport(trace);
  const bridge = createReaderBridge();
  const root = createRoot(host);
  root.render(<SessionReader bridge={bridge} />);
  if (import.meta.hot) import.meta.hot.dispose(() => { root.unmount(); bridge.destroy(); window.TraceViewport?.destroy(); });
}
