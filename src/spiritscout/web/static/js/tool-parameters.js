/* 参数首次展开时读取；并发合并、会话隔离，关闭的卡片不产生读取请求。 */
const ToolParameters = (() => {
  let session = null, generation = 0, active = 0;
  const queue = [], cache = new Map(), waiting = new Set(), inflight = new Map();
  function reset(id) { session = id; generation++; queue.length = 0; cache.clear(); waiting.clear(); inflight.clear(); }
  function show(entry, data) {
    if (!entry.element.isConnected || entry.session !== S.cur) return;
    entry.pre.textContent = data.available ? JSON.stringify(data.parameters, null, 2) : data.reason;
    entry.element.querySelector('.ar').textContent = data.available
      ? argBrief(entry.call.name, data.parameters) : '参数副本暂不可用';
  }
  function enqueue(entry) {
    if (entry.queued || entry.loaded || !entry.box.open || entry.session !== session) return;
    entry.queued = true; queue.push(entry); drain();
  }
  function drain() {
    while (active < 4 && queue.length) {
      const entry = queue.shift(), version = generation;
      entry.queued = false;
      if (!entry.element.isConnected || !entry.box.open || entry.session !== session) continue;
      active++;
      (async () => {
        const requestKey = `${version}:${entry.key}`;
        try {
          let request = inflight.get(requestKey);
          if (!request) {
            request = api(`/api/sessions/${encodeURIComponent(entry.session)}/tool-parameters?` +
              new URLSearchParams({tool:entry.call.name,digest:entry.digest}));
            inflight.set(requestKey, request);
          }
          const data = await request;
          if (version !== generation || entry.session !== session) return;
          entry.attempts++;
          if (data.available) { cache.set(entry.key, data); entry.loaded = true; waiting.delete(entry); }
          else if (data.retryable !== false && entry.attempts < 3 && !TERMINAL.has(S.state?.session?.status)) waiting.add(entry);
          show(entry, data);
        } catch (error) {
          if (version === generation && entry.session === session && entry.element.isConnected) {
            entry.pre.textContent = `参数读取失败：${error.message}。收起后重新展开可重试。`;
          }
        } finally { inflight.delete(requestKey); active--; drain(); }
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
    const entry = {element,box,pre,call,digest,session:S.cur,key:call.name+':'+digest,attempts:0};
    pre.textContent = '展开后读取调用参数';
    box.addEventListener('toggle', () => {
      if (!box.open) return;
      if (cache.has(entry.key)) { entry.loaded = true; show(entry,cache.get(entry.key)); return; }
      pre.textContent = '正在读取调用参数…'; enqueue(entry);
    });
  }
  function refresh() {
    for (const entry of waiting) { waiting.delete(entry); enqueue(entry); }
  }
  return {reset,attach,refresh};
})();
