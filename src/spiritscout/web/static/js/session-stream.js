"use strict";
/* SSE 连接生命周期与字节游标续传。 */
/** @param {boolean} [rebuild] */
function startStreams(rebuild = true) {
  stopStreams();
  if (!S.cur) return;
  if (rebuild) { S.evSeen = 0; SessionHistory.open(); return; }
  S.esTick = Date.now();
  // 滚动历史与 SSE 共用字节游标，重连直接 seek，不从文件头重扫。
  const sessionId = S.cur;
  const es = new EventSource(`/api/sessions/${sessionId}/events`
    + `?cursor=bytes&offset=${S.evSeen || 0}`);
  S.es = es;
  let received = S.evSeen;
  // 审计帧是具名事件（event: audit），onmessage 收不到——必须按事件名监听
  es.addEventListener("audit", m => {
    if (S.cur !== sessionId || S.es !== es) return;
    S.esTick = Date.now();
    try {
      const e = /** @type {import("./frontend-types").AuditEvent} */ (JSON.parse(m.data));
      // 去重游标用 SSE 事件 id（完整日志行的结尾字节偏移）；
      // 载荷 seq 是各写入实例各自计数，多写入者会撞车回退，不能用于去重
      const lid = Number(m.lastEventId) || 0;
      if (lid) {
        if (lid <= received) return;
        received = lid;
      }
      SessionHistory.enqueue(e, lid, sessionId);
      if (/^(?:agent\.(?:discovery|progress|tool_result)|task\.|decision\.|vuln\.|flag\.|platform\.|system\.|model\.call)/.test(e.type || "")) scheduleStateRefresh();
    } catch {}
  });
  es.addEventListener("done", async () => {
    await SessionHistory.drain();
    if (S.cur !== sessionId || S.es !== es) return;
    // 终态排空后主动关闭，防止 EventSource 每隔数秒重连并重复刷新状态。
    stopStreams();
    refreshState();
  });
  es.onerror = () => {};
  // 断流看门狗：合盖休眠/网络切换后 EventSource 会静默僵死（readyState 不可信），
  // 会话仍在运行却长时间无帧即判死，增量重连补齐缺口（已渲染行保留）
  S.esDog = setInterval(() => {
    if (S.view !== "session" || !S.cur || SessionHistory.isPast()) return;
    if (TERMINAL.has(S.state?.session?.status)) return;
    // 暂停态无帧是正常态（智能体已挂起），豁免看门狗避免空转重连
    if (S.state?.session?.paused) return;
    const dead = !S.es || S.es.readyState === EventSource.CLOSED;
    const stale = Date.now() - (S.esTick || 0) > 45000;
    if (dead || stale) startStreams(false);
  }, 15000);
}
function stopStreams() {
  SessionHistory.clearLiveQueue();
  if (S.es) { S.es.close(); S.es = null; }
  if (S.esDog) { clearInterval(S.esDog); S.esDog = null; }
}
