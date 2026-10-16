/* 参数按会话读取，原始审计继续封存；DOM 仅接收脱敏 JSON 文本。 */
const ToolParameters = (() => {
  let session = null, generation = 0, active = 0;
  const queue = [], cache = new Map(), waiting = new Set();
  function reset(id) { session = id; generation++; queue.length = 0; cache.clear(); waiting.clear(); }
  function show(entry, data) {
    if (!entry.element.isConnected || entry.session !== S.cur) return;
    entry.pre.textContent = data.available ? JSON.stringify(data.parameters, null, 2) : data.reason;
    entry.element.querySelector('.ar').textContent = data.available
      ? argBrief(entry.call.name, data.parameters) : "历史参数副本不可用";
  }
  async function drain() {
    while (active < 4 && queue.length) {
      const entry = queue.shift(), version = generation;
      active++;
      (async () => {
        try {
          const data = await api(`/api/sessions/${encodeURIComponent(entry.session)}/tool-parameters?` +
            new URLSearchParams({tool:entry.call.name,digest:entry.digest}));
          if (version !== generation || entry.session !== session) return;
          if (data.available) { cache.set(entry.key, data); waiting.delete(entry); }
          else if (!TERMINAL.has(S.state?.session?.status)) waiting.add(entry);
          show(entry, data);
        } catch (error) {
          if (version === generation && entry.session === session && entry.element.isConnected) {
            if (error.detail === 'Not Found') {
              entry.pre.textContent = '参数查看接口尚未加载，需要重载后端后刷新页面。';
              entry.element.querySelector('.ar').textContent = '等待参数接口加载';
              return;
            }
            entry.pre.textContent = `参数读取失败：${error.message}（点击工具标题重试）`;
            waiting.add(entry);
          }
        } finally { active--; drain(); }
      })();
    }
  }
  function attach(element, call) {
    const box = document.createElement('details'); box.className = 'tl-parameters';
    const title = document.createElement('summary'); title.textContent = '调用参数';
    const pre = document.createElement('pre'); box.append(title, pre);
    element.insertBefore(box, element.querySelector('.nt'));
    const args = call.arguments;
    if (args && !args._redacted) { pre.textContent = JSON.stringify(args,null,2); return; }
    const digest = args?.digest;
    if (!/^[0-9a-f]{64}$/.test(digest || '')) { pre.textContent = '此历史调用未保存可读取的参数副本'; return; }
    const entry = {element,pre,call,digest,session:S.cur,key:call.name+':'+digest};
    if (cache.has(entry.key)) { show(entry,cache.get(entry.key)); return; }
    pre.textContent = '正在读取调用参数…'; queue.push(entry); drain();
    element.querySelector('button.hd').addEventListener('click', () => {
      if (waiting.has(entry)) { waiting.delete(entry); queue.push(entry); drain(); }
    });
  }
  function refresh() {
    for (const entry of waiting) {
      waiting.delete(entry);
      if (entry.element.isConnected && entry.session === S.cur) queue.push(entry);
    }
    drain();
  }
  return {reset,attach,refresh};
})();
