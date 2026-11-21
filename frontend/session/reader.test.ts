import { test } from 'node:test';
import assert from 'node:assert/strict';
import { entryKind, matchesEntry } from './reader.ts';
import type { ReadingEntry } from './reader.ts';
import { createReaderBridge } from './bridge.ts';

test('outline classifies records without hiding ordinary progress', () => {
  for (const [type, part, status, classes, expected] of [
    ['agent.progress', 'continuation', 'running', 'tl-task', 'progress'],
    ['task.failed', 'outcome', 'failed', 'tl-task', 'attention'],
    ['agent.ask_user', '', '', 'tl', 'attention'],
    ['user.message', '', '', 'tl tl-user', 'message'],
    ['agent.reply', '', '', 'tl tl-conv', 'message'],
    ['task.done', 'summary', 'done', 'tl-task', 'result'],
    ['agent.turn', 'continuation', 'waiting', 'tl-task', 'attention'],
    ['model.retry_scheduled', '', '', 'tl-retry', 'attention'],
    ['model.failed', '', '', 'tl-sys', 'attention'],
  ]) {
    const kind = entryKind(type!, part!, status!, classes!);
    assert.equal(kind, expected);
    assert.equal(matchesEntry({ kind, title: '', text: '', actor: '', id: '', time: '' } as ReadingEntry, ''), true);
  }
});

test('search requires all terms and searches actor and full text without interpreting HTML', () => {
  const entry: ReadingEntry = { id: '1', kind: 'result', title: '权限检查', text: 'HTTP 403 <script>', actor: '贯甲', time: '' };
  assert.ok(matchesEntry(entry, '贯甲 http'));
  assert.ok(matchesEntry(entry, '权限 <script>'));
  assert.equal(matchesEntry(entry, '权限 200'), false);
});

// 模拟投影和观察器的时序；真实布局与翻页锚点另用浏览器验证。
class ReaderElement extends EventTarget {
  dataset: Record<string, string> = {};
  children: ReaderElement[] = [];
  parentElement: ReaderElement | null = null;
  hidden = false;
  textContent = '';
  lang = 'zh-CN';
  classes = new Set<string>();
  classList = {
    toggle: (name: string, force: boolean) => { if (force) this.classes.add(name); else this.classes.delete(name); },
    remove: (...names: string[]) => names.forEach(name => this.classes.delete(name)),
  };
  get className() { return [...this.classes].join(' '); }
  querySelector() { return null; }
  querySelectorAll() { return this.children.filter(row => row.classes.has('tl')); }
  closest() { return this.classes.has('tl') ? this : this.parentElement; }
  append(row: ReaderElement) { this.children.push(row); row.parentElement = this; }
}

test('reader filters staged windows before commit and live mutations before snapshot refresh', t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const root = new ReaderElement(), trace = new ReaderElement(), html = new ReaderElement();
  root.dataset.sessionId = 'one';
  const media = Object.assign(new EventTarget(), { matches: false });
  let notify!: (records: MutationRecord[]) => void;
  let disconnected = false;
  const replacements = {
    Element: ReaderElement,
    document: { documentElement: html, querySelector: (selector: string) => selector === '#view-session' ? root : trace },
    matchMedia: () => media,
    MutationObserver: class {
      constructor(callback: typeof notify) { notify = callback; }
      observe() {}
      disconnect() { disconnected = true; }
    },
  };
  for (const [key, value] of Object.entries(replacements)) {
    const previous = Object.getOwnPropertyDescriptor(globalThis, key);
    Object.defineProperty(globalThis, key, { configurable: true, value });
    t.after(() => { if (previous) Object.defineProperty(globalThis, key, previous); else Reflect.deleteProperty(globalThis, key); });
  }
  const row = (part: string, text: string) => {
    const node = new ReaderElement();
    node.classes.add('tl'); node.classes.add('tl-task');
    node.dataset.taskPart = part; node.dataset.taskStatus = 'running'; node.textContent = text;
    return node;
  };
  const filtered = (node: ReaderElement) => node.classes.has('reader-filtered');
  const mutate = (node: ReaderElement) => notify([{ target: node } as unknown as MutationRecord]);
  const progress = row('continuation', '扫描中'), result = row('summary', '已完成结论');
  trace.append(progress); trace.append(result);
  const bridge = createReaderBridge();
  t.after(() => bridge.destroy());
  bridge.filter('结论');
  assert.ok(filtered(progress)); assert.equal(filtered(result), false);

  const stage = new ReaderElement(), older = row('continuation', '早期扫描'), conclusion = row('summary', '结论');
  stage.append(older); stage.append(conclusion);
  trace.dispatchEvent(new CustomEvent('reader:render', { detail: stage }));
  assert.ok(filtered(older), '新窗口在插入可见 DOM 与恢复滚动前已应用搜索条件');
  assert.equal(filtered(conclusion), false);
  trace.children = stage.children;
  for (const node of trace.children) node.parentElement = trace;
  mutate(trace);
  assert.ok(filtered(older));
  assert.equal(bridge.getSnapshot().entries[0]?.text, '扫描中', '目录快照仍可合并更新');

  older.dataset.taskStatus = 'failed'; mutate(older);
  assert.ok(filtered(older), '记录类型变化不会越过搜索条件');
  older.textContent = '结论待核实'; mutate(older);
  assert.equal(filtered(older), false, '匹配内容出现后在绘制前显示');
  older.textContent = '早期扫描'; mutate(older);
  assert.ok(filtered(older));
  assert.equal(filtered(conclusion), false, '搜索立即基于当前 DOM 而非旧窗口快照');
  conclusion.textContent = '扫描结束'; mutate(conclusion);
  assert.ok(filtered(conclusion), '正文变化在绘制前更新搜索匹配');
  t.mock.timers.tick(120);
  assert.equal(bridge.getSnapshot().entries[1]?.text, '扫描结束');

  root.dataset.sessionId = 'two';
  const next = new ReaderElement(), nextProgress = row('continuation', '新会话'); next.append(nextProgress);
  trace.dispatchEvent(new CustomEvent('reader:render', { detail: next }));
  assert.equal(filtered(nextProgress), false, '新会话提交不能沿用旧会话的搜索条件');
  mutate(root); t.mock.timers.tick(120);
  bridge.filter('结论');
  const live = row('continuation', '实时扫描'); trace.append(live); mutate(trace);
  assert.ok(filtered(live), '实时新增行在目录刷新前就完成搜索');
  bridge.filter('');
  assert.equal(filtered(live), false, '清除搜索后恢复全部记录');
  const other = row('continuation', '其他角色插入'); other.dataset.agent = 'two';
  trace.append(other); mutate(trace);
  const outcome = row('summary', '任务收口'); trace.append(outcome); mutate(outcome);
  assert.equal(filtered(other), false, '默认不隐藏其他角色');
  assert.equal(filtered(outcome), false, '默认同时保留进展与结论');
  assert.deepEqual(trace.children.slice(-3).map(node => node.textContent), ['实时扫描', '其他角色插入', '任务收口'], '显示沿用原始时间顺序');
  bridge.filter('无匹配项');
  assert.ok(filtered(live));
  bridge.destroy();
  assert.ok(disconnected);
  assert.equal(filtered(live), false, '销毁时也清理尚未进入快照的新行');
  trace.dispatchEvent(new CustomEvent('reader:render', { detail: stage }));
  assert.equal(filtered(conclusion), false, '销毁后不再响应历史投影');
});

test('outline titles describe content rather than repeating actor names', async () => {
  const { readingTitle, outlineSections } = await import('./reader.ts');
  assert.equal(readingTitle('', '解题报告已生成', '结果与交付'), '解题报告已生成');
  assert.equal(readingTitle('匿名鉴权边界', '很长的工具明细', '任务进展'), '匿名鉴权边界');
  assert.equal(readingTitle('', '', '运行记录'), '运行记录');
  assert.ok(readingTitle('', '字'.repeat(100), '记录').endsWith('…'));
  assert.equal(entryKind('report.ready', '', '', 'tl tl-conv'), 'result');
  const entries = ['progress', 'result', 'attention', 'message'].map((kind, i) => ({ id: String(i), kind, title: kind, text: '', actor: '灵枢', time: '' } as ReadingEntry));
  assert.deepEqual(outlineSections(entries).map(section => section.kind), ['message', 'result', 'attention', 'progress']);
});
