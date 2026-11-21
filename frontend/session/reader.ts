export type EntryKind = 'message' | 'progress' | 'result' | 'attention' | 'activity';
export interface ReadingEntry {
  id: string;
  title: string;
  text: string;
  kind: EntryKind;
  actor: string;
  time: string;
}

export function entryKind(type: string, part: string, status: string, classes: string): EntryKind {
  if (status === 'failed' || status === 'waiting' || /(?:error|failed|blocked|ask_user|question|approval|retry_scheduled|retrying)/.test(type)) return 'attention';
  if (part === 'summary' || part === 'outcome' || /(?:report\.ready|file\.shared|master\.done)/.test(type) || /(?:vuln\.confirmed|flag\.(?:accepted|submitted))/.test(type)) return 'result';
  if (/\btl-(?:user|conv)\b/.test(classes)) return 'message';
  if (part === 'continuation') return 'progress';
  return 'activity';
}

export function matchesEntry(entry: ReadingEntry, query: string): boolean {
  const terms = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  const haystack = `${entry.title}\n${entry.actor}\n${entry.text}`.toLocaleLowerCase();
  return terms.every(term => haystack.includes(term));
}

// 从已有正文生成定位标题，不推断执行阶段，也不额外请求模型做摘要。
export function readingTitle(heading: string, body: string, fallback: string): string {
  const text = (heading || body).replace(/\s+/g, ' ').trim();
  return text ? [...text].slice(0, 88).join('') + ([...text].length > 88 ? '…' : '') : fallback;
}

export function outlineSections(entries: ReadingEntry[]) {
  return (['message', 'result', 'attention', 'progress', 'activity'] as const)
    .map(kind => ({ kind, entries: entries.filter(entry => entry.kind === kind) }))
    .filter(section => section.entries.length);
}
