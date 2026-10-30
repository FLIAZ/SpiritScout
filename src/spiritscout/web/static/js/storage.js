"use strict";
/* 存储清理只在用户确认后提交；关闭弹窗不取消服务端任务，再打开可查看结果。 */
const SessionStorage = (() => {
  let version = 0, session = "", timer = null, preview = null;
  const node = name => $(`#storage-${name}`);
  const bytes = value => {
    let n = Number(value || 0), unit = 0;
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    while (n >= 1024 && unit < units.length - 1) { n /= 1024; unit++; }
    return `${n.toFixed(unit ? 2 : 0)} ${units[unit]}`;
  };
  function stop() { clearTimeout(timer); timer = null; }
  function message(text, error = false) {
    node("status").textContent = text;
    node("status").classList.toggle("storage-error", error);
  }
  function ready() {
    node("clean").disabled = !preview || !!preview.blocked_reason ||
      (!node("database").checked && !node("caches").checked);
  }
  function result(job) {
    if (job.status === "failed") {
      message(`清理未完成：${job.error || "请重新扫描后重试"}`, true);
    } else {
      message(`已释放 ${bytes(job.released_bytes)}。` +
        (job.warnings?.length ? ` ${job.warnings.join("；")}` : "会话、对话与成果已保留。"), job.status === "partial");
    }
  }
  async function poll(id, token) {
    if (token !== version) return;
    try {
      const job = await api(`/api/sessions/${encodeURIComponent(id)}/storage/job`);
      if (token !== version) return;
      if (job.status === "running") {
        message(job.phase || "正在清理…");
        timer = setTimeout(() => poll(id, token), 1200);
      } else {
        await inspect(id, token, job);
      }
    } catch (e) {
      if (token !== version) return;
      message(`暂时无法读取进度：${e.message}。正在重试；后台任务继续执行。`, true);
      timer = setTimeout(() => poll(id, token), 3000);
    }
  }
  async function inspect(id, token, finished = null) {
    stop(); preview = null; ready();
    node("details").textContent = "正在分析存储…";
    try {
      const [data, job] = await Promise.all([
        api(`/api/sessions/${encodeURIComponent(id)}/storage`),
        finished ? Promise.resolve(finished) : api(`/api/sessions/${encodeURIComponent(id)}/storage/job`),
      ]);
      if (token !== version) return;
      preview = data;
      const db = data.database || {};
      node("details").innerHTML = `<dl class="storage-stats">
        <div><dt>会话总占用</dt><dd>${bytes(data.bytes)}</dd></div>
        <div><dt>数据库文件</dt><dd>${bytes(db.bytes)}</dd></div>
        <div><dt>下载缓存</dt><dd>${bytes(data.cache_bytes)}</dd></div>
        <div><dt>数据库可复用空闲页</dt><dd>${bytes(db.reusable_bytes)}</dd></div></dl>
        <p>可清理已投递事件 ${Number(db.delivered_events || 0)} 条、旧 DAG 快照 ${Number(db.old_dag_versions || 0)} 个。
        检查点共 ${Number(db.checkpoints || 0)} 个，清理时保留各恢复键的最新依赖链。</p>
        <p class="muted">释放量在完成后按实际磁盘占用计算；数据库收缩需要额外空闲空间。</p>`;
      if (job.status === "running") {
        preview.blocked_reason = "正在清理存储";
        ready(); poll(id, token);
      } else {
        ready();
        if (["completed", "partial", "failed"].includes(job.status)) result(job);
        else message(data.blocked_reason || "选择清理内容，确认后执行。旧 DAG 历史和旧检查点清理后无法回溯。");
      }
    } catch (e) {
      if (token !== version) return;
      preview = null; ready();
      node("details").textContent = "分析未完成，可点击重新扫描。";
      message(e.message, true);
    }
  }
  async function open(id = "") {
    const token = ++version;
    stop(); preview = null; ready();
    session = id;
    const dlg = node("dlg");
    if (!dlg.open) dlg.showModal();
    node("select").disabled = true;
    node("details").textContent = "正在扫描会话占用…";
    message("只扫描占用，不会自动删除数据。");
    try {
      const data = await api("/api/storage/sessions");
      if (token !== version) return;
      node("total").textContent = `全部会话占用 ${bytes(data.total_bytes)} · 按占用从大到小排列`;
      node("select").innerHTML = data.sessions.map(s =>
        `<option value="${esc(s.session_id)}">${esc(s.title)} · ${bytes(s.bytes)}${s.blocked_reason ? ` · ${esc(s.blocked_reason)}` : ""}</option>`).join("");
      if (!data.sessions.length) {
        node("details").textContent = "还没有会话数据。";
        message("无需清理"); return;
      }
      session = data.sessions.some(s => s.session_id === id) ? id : data.sessions[0].session_id;
      node("select").value = session;
      node("select").disabled = false;
      selxSync(node("select"));
      await inspect(session, token);
    } catch (e) {
      if (token !== version) return;
      node("details").textContent = "扫描失败，可点击重新扫描。";
      message(e.message, true);
    } finally { selxSync(node("select")); }
  }
  async function clean() {
    if (node("clean").disabled || !preview || preview.blocked_reason) return;
    const id = session, token = ++version;
    stop(); preview = null; ready();
    message("正在提交清理任务…");
    try {
      await api(`/api/sessions/${encodeURIComponent(id)}/storage/cleanup`, {
        method: "POST", body: JSON.stringify({confirm:true,
          database:node("database").checked, caches:node("caches").checked}),
      });
      if (token === version) poll(id, token);
    } catch (e) {
      if (token !== version) return;
      // 请求响应丢失时任务可能已启动；重新查询，不直接再次提交。
      await inspect(id, token);
      if (token === version) message(`提交结果：${e.message}。请以当前任务状态为准。`, true);
    }
  }
  node("close").onclick = () => node("dlg").close();
  node("dlg").addEventListener("close", () => { ++version; stop(); preview = null; });
  node("refresh").onclick = () => open(session);
  node("select").onchange = () => { session = node("select").value; inspect(session, ++version); };
  node("database").onchange = node("caches").onchange = ready;
  node("clean").onclick = clean;
  $("#btn-storage").onclick = () => open();
  return {open, bytes};
})();
