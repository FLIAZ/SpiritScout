"use strict";
/* 单一调度器：SSE 负责即时反馈，轮询负责静默期和断线兜底。 */
const ConsolePolling = (() => {
  function create({refreshList, refreshState, isSession, isRunning, isVisible,
                   now = Date.now, later = setTimeout, cancel = clearTimeout}) {
    let timer = null, stopped = true, lastList = 0, lastState = 0;
    const invoke = callback => { Promise.resolve().then(callback).catch(() => {}); };
    function tick(force = false) {
      if (stopped) return;
      const time = now(), visible = isVisible();
      const listInterval = visible ? 5000 : 30000;
      const stateInterval = visible ? (isRunning() ? 3000 : 15000) : 30000;
      if (force || time - lastList >= listInterval) { lastList = time; invoke(refreshList); }
      if (isSession() && (force || time - lastState >= stateInterval)) {
        lastState = time; invoke(refreshState);
      }
      timer = later(() => tick(), visible ? 1000 : 5000);
    }
    return {
      listRequested() { lastList = now(); },
      stateRequested() { lastState = now(); },
      start() { if (!stopped) return; stopped = false; lastList = lastState = now(); tick(); },
      stop() { stopped = true; if (timer !== null) cancel(timer); timer = null; },
      wake() { if (stopped) return; if (timer !== null) cancel(timer); tick(true); },
    };
  }
  return {create};
})();
