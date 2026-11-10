"use strict";
/* 会话代理的开关与手动接入信息；离页清除口令显示，不改变后台采集状态。 */
const TrafficProxy = (() => {
  function mount(root, session) {
    let alive = true, version = 0, busy = false, lastError = "", unsupported = false;
    const base = `/api/sessions/${encodeURIComponent(session)}/traffic/proxy`;
    root.innerHTML = I18n.html`<details class="traffic-proxy-controls"><summary>可选：录制命令流量 <span class="muted" data-proxy-status role="status">正在读取…</span></summary>
      <div data-proxy-actions hidden><header class="traffic-head"><strong>会话代理录制</strong>
      <div><button class="btn sm" data-proxy-enable disabled>启用代理录制</button><button class="btn sm" data-proxy-stop disabled>停止代理</button></div></header>
      <p class="muted">启用后，新启动的 Linux 沙箱命令和本地工具自动使用本会话代理与 CA；遵守代理配置的 HTTP/HTTPS 流量会留下原文。内置 HTTP 探测保持原有记录通道。</p>
      <details><summary>代理配置与手动接入</summary><label>上游出口代理（可选）<input type="password" maxlength="2048" data-proxy-upstream autocomplete="off" placeholder="留空直连；支持 http://、https://、socks5://"></label>
        <p class="muted">仅信任会话 CA，不安装系统证书。启用会介入 TLS 握手；停止代理会结束经过它的连接，不自动重放请求。</p>
        <button class="btn sm" data-proxy-env disabled>显示手动接入配置</button> <a class="btn sm" data-proxy-ca hidden>下载会话 CA</a>
        <div data-proxy-environment hidden><p class="muted">以下包含本会话代理口令，请勿公开；进程外工具需自行使用这些环境变量。</p><pre></pre></div>
      </details></div><p class="traffic-notice" data-proxy-message role="status"></p></details>`;
    const get = selector => root.querySelector(selector);
    const tell = (text, error = false) => { get("[data-proxy-message]").textContent = text; get("[data-proxy-message]").classList.toggle("traffic-error", error); };
    function render(data) {
      get("[data-proxy-actions]").hidden = !data.available;
      const names = {starting:I18n.t("正在启动"), running:I18n.t("录制中"), stopping:I18n.t("正在停止"), stopped:I18n.t("未启用"), failed:I18n.t("代理异常")};
      get("[data-proxy-status]").textContent = I18n.t` · ${names[data.status] || data.status}${data.status === "running" && data.address ? " · " + data.address : ""} · 本次 ${data.records} 条`;
      const active = ["starting", "running", "stopping"].includes(data.status);
      get("[data-proxy-enable]").disabled = busy || active || !data.available;
      get("[data-proxy-stop]").disabled = busy || !active;
      if (data.locked) {
        get('[data-proxy-enable]').disabled = true;
        get('[data-proxy-stop]').disabled = true;
      }
      get("[data-proxy-upstream]").disabled = busy || active;
      get("[data-proxy-env]").disabled = busy || data.status !== "running";
      get("[data-proxy-ca]").hidden = data.status !== "running";
      get("[data-proxy-ca]").href = base + "/ca";
      if (data.status !== "running") { get("[data-proxy-environment]").hidden = true; get("[data-proxy-environment] pre").textContent = ""; }
      const unavailable = data.available ? "" : I18n.t("可选代理未构建。普通工具记录、PCAP 导入与数据包分析仍可使用；需要录制命令的 HTTP/HTTPS 流量时，运行 scripts/build-traffic-proxy.sh。");
      tell(data.error || lastError || (data.locked ? `${data.binding.name} · 固定出口 · ${data.coverage}` : unavailable), Boolean(data.error || lastError));
    }
    async function refresh() {
      if (unsupported) return;
      const token = ++version;
      try { const data = await api(base); if (alive && token === version) render(data); }
      catch (error) {
        if (!alive || token !== version) return;
        get("[data-proxy-actions]").hidden = true;
        const legacy = error.status === 422 && Array.isArray(error.detail) && error.detail.some(item => item.input === "proxy" && item.loc?.includes("record_id"));
        if (legacy || [404, 405].includes(error.status)) {
          unsupported = true;
          get("[data-proxy-status]").textContent = I18n.t("· 当前后端未提供代理功能");
          tell(I18n.t("当前服务尚未加载代理接口。普通流量记录照常可用；如需此可选功能，请在运行任务结束后重启后端，再刷新页面。"));
        } else {
          get("[data-proxy-status]").textContent = I18n.t("· 暂时不可用");
          tell(I18n.t`代理状态读取失败：${error.message}`, true);
        }
      }
    }
    async function change(method) {
      if (busy) return;
      lastError = "";
      busy = true;
      get("[data-proxy-enable]").disabled = get("[data-proxy-stop]").disabled = true;
      tell(method === "POST" ? I18n.t("正在启动本会话代理…") : I18n.t("正在停止代理并保存最后的证据…"));
      try {
        const options = {method};
        if (method === "POST") options.body = JSON.stringify({upstream:get("[data-proxy-upstream]").value.trim()});
        await api(base, options);
      } catch (error) { if (alive) { lastError = I18n.t`代理操作失败：${error.message}`; tell(lastError, true); } }
      finally { busy = false; if (alive) await refresh(); }
    }
    get("[data-proxy-enable]").onclick = () => change("POST");
    get("[data-proxy-stop]").onclick = () => change("DELETE");
    get("[data-proxy-env]").onclick = async () => {
      const token = version;
      try {
        const data = await api(base + "/environment");
        if (!alive || token !== version) return;
        get("[data-proxy-environment] pre").textContent = JSON.stringify(data.environment, null, 2);
        get("[data-proxy-environment]").hidden = false;
      } catch (error) { if (alive) tell(error.message, true); }
    };
    return {session, refresh, close() { alive = false; ++version; root.replaceChildren(); }};
  }
  return {mount};
})();
