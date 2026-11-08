"use strict";
/* 真实帧列表 / 协议树 / 原始字节三窗格；每个实例绑定一个会话。 */
const PacketBrowser = (() => {
  const escText = value => esc(value);
  const statusNames = {starting:I18n.t("准备中"), running:I18n.t("解析中"), completed:I18n.t("已完成"), stopped:I18n.t("已停止"), failed:I18n.t("失败"), interrupted:I18n.t("已中断")};
  function hexDump(hex) {
    const data = String(hex || "").match(/../g) || [], lines = [];
    for (let offset = 0; offset < data.length; offset += 16) {
      const row = data.slice(offset, offset + 16);
      lines.push(offset.toString(16).padStart(8, "0") + "  " + row.join(" ").padEnd(47) + "  " +
        row.map(value => { const n = parseInt(value, 16); return n >= 32 && n <= 126 ? String.fromCharCode(n) : "."; }).join(""));
    }
    return lines.join("\n");
  }
  function treeHtml(layers) {
    const label = (name, key) => (key.startsWith(`${name}_${name}_`) ? key.slice(name.length * 2 + 2) : key).replaceAll("_", " ");
    return Object.entries(layers || {}).map(([name, fields]) => `<details class="packet-layer" ${["http", "http2", "dns", "websocket", "data"].includes(name) ? "open" : ""}>
      <summary>${escText(name.toUpperCase())}</summary><dl>${Object.entries(fields).map(([key, value]) =>
        `<dt title="${escText(key)}">${escText(label(name, key))}</dt><dd>${escText(Array.isArray(value) ? value.join("\n") : value)}</dd>`).join("")}</dl></details>`).join("") || I18n.html('<p class="muted">无协议字段。</p>');
  }
  function mount(root, session, captureId) {
    let alive = true, version = 0, detailVersion = 0, items = [], selected = captureId || "", before = null,
      next = null, history = [], expression = "", displayed = "", selectedPacket = null, loadedSignature = "";
    root.innerHTML = I18n.html`<section class="packet-workbench">
      <p class="muted">离线数据包时间线 · 原始内容未脱敏 · 选择一行查看协议、负载与原始字节</p>
      <div class="packet-source"><label>流量附件<select data-captures aria-label="流量附件"></select></label><a class="btn sm" data-export hidden>下载原始附件</a><button type="button" class="btn sm" data-packet-refresh>刷新解析状态</button><span data-capture-status role="status"></span></div>
      <form class="packet-display-filter"><label>Wireshark 显示过滤器<input name="display_filter" maxlength="512" spellcheck="false" placeholder="例如 dns 或 tcp.port == 443 或 websocket"></label><button class="btn sm">应用过滤</button></form>
      <p class="traffic-notice" data-packet-message role="status"></p>
      <div class="packet-list traffic-table-wrap" data-packet-list></div>
      <div class="traffic-pagination"><span class="muted" data-packet-page></span><button class="btn sm" data-packet-prev disabled>上一页</button><button class="btn sm" data-packet-next disabled>下一页</button></div>
      <section class="packet-inspector" data-packet-inspector hidden aria-label="数据包详情">
        <header class="traffic-head"><h3 data-packet-title>数据包详情</h3><button class="btn sm" data-close-packet>收起详情</button></header>
        <p class="muted" data-packet-origin></p><div data-follow-controls></div>
        <div class="packet-panes"><section aria-label="协议树"><h4>协议详情</h4><div data-protocol-tree></div></section>
          <section aria-label="原始字节"><h4>应用负载 · 文本</h4><pre data-packet-payload></pre><h4>原始字节 · Hex / ASCII</h4><pre data-packet-hex></pre><p class="muted" data-byte-note></p></section></div>
        <section data-stream-view hidden aria-label="流追踪"></section>
      </section></section>`;
    const get = selector => root.querySelector(selector);
    const base = `/api/sessions/${encodeURIComponent(session)}/traffic`;
    const notice = (text, error = false) => { get("[data-packet-message]").textContent = text; get("[data-packet-message]").classList.toggle("traffic-error", error); };
    function clearDetail() {
      ++detailVersion; selectedPacket = null; get("[data-packet-inspector]").hidden = true;
      get("[data-packet-list]").querySelectorAll("[data-packet-row]").forEach(row => { row.classList.toggle("packet-selected", false); row.setAttribute("aria-selected", "false"); });
      get("[data-stream-view]").hidden = true; get("[data-stream-view]").replaceChildren();
    }
    function reset() { before = null; next = null; history = []; loadedSignature = ""; clearDetail(); }
    const signature = () => {
      const row = items.find(item => item.capture_id === selected);
      return [selected, row?.packet_count, row?.status, expression, before].join("\u0000");
    };
    function controls() {
      const row = items.find(item => item.capture_id === selected);
      get("[data-captures]").disabled = !items.length;
      for (const [selector, label] of [["[data-captures]", I18n.t("流量附件")]]) {
        const select = get(selector);
        if (typeof selxSync === "function") selxSync(select);
        const button = select.closest?.(".selx")?.querySelector(".selx-btn");
        if (button) button.setAttribute("aria-label", label);
      }
      get("[data-capture-status]").textContent = row ? I18n.t`${statusNames[row.status] || row.status} · ${row.packet_count} 包${row.limited ? I18n.t(" · 已达采集 / 解析上限") : ""}${row.error ? " · " + row.error : ""}` : I18n.t("尚无流量附件");
      const link = get("[data-export]");
      link.hidden = !row || ["running", "starting"].includes(row.status);
      if (row) link.href = `${base}/captures/${encodeURIComponent(row.capture_id)}/download`;
    }
    async function refresh() {
      const token = ++version;
      try {
        const data = await api(`${base}/captures`);
        if (!alive || token !== version) return;
        items = captureId ? data.items.filter(item => item.capture_id === captureId) : data.items.filter(item => item.source !== "live");
        if (!items.some(item => item.capture_id === selected)) { selected = items[0]?.capture_id || ""; reset(); }
        const options = items.map(item => I18n.html`<option value="${escText(item.capture_id)}">${escText(item.name || item.capture_id.slice(0, 8))} · ${statusNames[item.status] || escText(item.status)} · ${item.packet_count} 包</option>`).join("");
        if (get("[data-captures]").innerHTML !== options) get("[data-captures]").innerHTML = options;
        get("[data-captures]").value = selected;
        controls();
        if (!alive || token !== version) return;
        if (!before && signature() !== loadedSignature) await load();
        return {pending: items.some(item => ["running", "starting"].includes(item.status))};
      } catch (error) { if (alive && token === version) notice(I18n.t`数据包读取失败：${error.message}`, true); }
    }
    let rowsVersion = 0;
    async function load() {
      const token = ++rowsVersion, capture = selected;
      const list = get("[data-packet-list]");
      get("[data-packet-prev]").disabled = true; get("[data-packet-next]").disabled = true;
      if (displayed !== capture) { list.replaceChildren(); displayed = capture; }
      if (!capture) { list.replaceChildren(); notice(I18n.t("尚无可显示的数据包。附件正在解析或未包含可识别的网络帧。")); return; }
      const params = new URLSearchParams({capture_id:capture, limit:"100", display_filter:expression, order:"asc"});
      if (before) params.set("before", before);
      try {
        const data = await api(`${base}/packets?${params}`);
        if (!alive || token !== rowsVersion || selected !== capture) return;
        loadedSignature = signature();
        next = data.has_more ? data.next_before : null;
        get("[data-packet-prev]").disabled = !history.length; get("[data-packet-next]").disabled = !next;
        get("[data-packet-page]").textContent = I18n.t`第 ${history.length + 1} 页 · ${data.items.length} 包`;
        get('[name="display_filter"]').removeAttribute("aria-invalid");
        const color = protocol => ["TCP","UDP","DNS","HTTP","HTTP2","TLS","WEBSOCKET","ICMP","ARP"].includes(protocol) ? protocol.toLowerCase() : "other";
        list.innerHTML = I18n.html`<table><thead><tr><th>No.</th><th>相对时间（s）</th><th>源地址</th><th>目标地址</th><th>协议</th><th>长度</th><th>信息</th></tr></thead><tbody>${data.items.map(packet =>
          I18n.html`<tr data-packet-row="${packet.id}" class="packet-proto-${color(packet.protocol)}${String(selectedPacket?.id) === String(packet.id) ? " packet-selected" : ""}"><td><button class="traffic-link" data-packet="${packet.id}" aria-label="查看数据包 ${packet.number}">${packet.number}</button></td>
          <td class="mono">${Number(packet.relative_time).toFixed(6)}</td><td class="mono">${escText(packet.source)}${packet.source_port === null ? "" : ":" + packet.source_port}</td>
          <td class="mono">${escText(packet.destination)}${packet.destination_port === null ? "" : ":" + packet.destination_port}</td><td>${escText(packet.protocol)}</td><td>${packet.length}</td><td class="packet-info">${escText(packet.info)}</td></tr>`).join("")}</tbody></table>`;
        list.querySelectorAll("[data-packet]").forEach(button => button.onclick = () => detail(button.dataset.packet));
        list.querySelectorAll("[data-packet-row]").forEach(row => row.onclick = event => { if (!event.target.closest("button")) detail(row.dataset.packetRow); });
        if (!selectedPacket && data.items.length) await detail(data.items[0].id);
        notice(data.items.length ? I18n.t("点击包编号查看协议树和原始字节。数据包未脱敏；加密负载不会自动变成明文。") : I18n.t("没有符合条件的数据包。"));
      } catch (error) {
        if (alive && token === rowsVersion) { notice(I18n.t`数据包读取失败：${error.message}`, true); get('[name="display_filter"]').setAttribute("aria-invalid", "true"); }
      }
    }
    async function detail(id) {
      const token = ++detailVersion, capture = selected;
      try {
        const packet = await api(`${base}/packets/${id}`);
        if (!alive || token !== detailVersion || capture !== selected) return;
        selectedPacket = packet;
        get("[data-packet-list]").querySelectorAll("[data-packet-row]").forEach(row => {
          const active = String(row.dataset.packetRow) === String(packet.id);
          row.classList.toggle("packet-selected", active); row.setAttribute("aria-selected", String(active));
        });
        get("[data-packet-title]").textContent = I18n.t`数据包 #${packet.number} · ${packet.protocol}`;
        get("[data-packet-origin]").textContent = I18n.t`${packet.source} → ${packet.destination} · ${packet.length} 字节 · ${new Date(packet.time * 1000).toLocaleString(I18n.dateLocale, {hour12:false})} · 离线附件`;
        get("[data-packet-payload]").textContent = packet.payload_text || I18n.t("（无可显示的传输层负载；TLS 等加密内容仍为密文，请查看协议字段或原始字节）");
        get("[data-protocol-tree]").innerHTML = treeHtml(packet.layers);
        get("[data-packet-hex]").textContent = hexDump(packet.raw_hex);
        get("[data-byte-note]").textContent = `${packet.raw_truncated ? I18n.t("原始字节预览已截断。") : ""}${packet.captured_length < packet.length ? I18n.t("采集长度小于线上帧长度。") : ""}${packet.fields_limited ? I18n.t("协议树字段有界展示。") : ""}${packet.payload_truncated ? I18n.t("负载预览已截断。") : ""}`;
        get("[data-stream-view]").hidden = true;
        get("[data-follow-controls]").innerHTML = ["tcp", "udp"].filter(proto => packet[proto + "_stream"] !== null).map(proto =>
          I18n.html`<button type="button" class="btn sm" data-follow="${proto}">追踪 ${proto.toUpperCase()} 流 #${packet[proto + "_stream"]}</button>`).join(" ");
        get("[data-follow-controls]").querySelectorAll("button").forEach(button => button.onclick = () => follow(packet, button.dataset.follow));
        get("[data-packet-inspector]").hidden = false;
      } catch (error) { if (alive && token === detailVersion) notice(I18n.t`数据包详情读取失败：${error.message}`, true); }
    }
    async function follow(packet, protocol) {
      const token = detailVersion, stream = packet[protocol + "_stream"];
      try {
        const data = await api(`${base}/captures/${packet.capture_id}/follow?protocol=${protocol}&stream=${stream}`);
        if (!alive || token !== detailVersion || selectedPacket !== packet) return;
        const box = get("[data-stream-view]");
        const render = mode => {
          box.innerHTML = I18n.html`<header class="traffic-head"><h4>${protocol.toUpperCase()} 流 #${stream}</h4><button class="btn sm" data-stream-mode>${mode === "text" ? I18n.t("显示 Hex") : I18n.t("显示文本")}</button></header>
            <p class="muted">${data.nodes.map(escText).join(" ↔ ")} · tshark 重组${data.truncated ? I18n.t(" · 展示已截断") : ""}</p>` + data.segments.map(segment =>
              `<div class="packet-stream-direction packet-direction-${segment.direction}"><small>${segment.direction === 0 ? I18n.t("节点 0 → 节点 1") : I18n.t("节点 1 → 节点 0")}</small><pre>${escText(mode === "text" ? segment.text : hexDump(segment.hex))}</pre></div>`).join("");
          box.querySelector("[data-stream-mode]").onclick = () => render(mode === "text" ? "hex" : "text");
        };
        render("text"); box.hidden = false;
      } catch (error) { if (alive && token === detailVersion) notice(I18n.t`流追踪失败：${error.message}`, true); }
    }
    get("[data-packet-refresh]").onclick = () => refresh();
    get("[data-captures]").onchange = () => { ++version; selected = get("[data-captures]").value; reset(); controls(); load(); };
    get(".packet-display-filter").onsubmit = event => { event.preventDefault(); expression = get('[name="display_filter"]').value.trim(); reset(); get("[data-packet-list]").replaceChildren(); load(); };
    get("[data-packet-prev]").onclick = () => { if (history.length) { before = history.pop(); clearDetail(); load(); } };
    get("[data-packet-next]").onclick = () => { if (next) { history.push(before); before = next; clearDetail(); load(); } };
    get("[data-close-packet]").onclick = clearDetail;
    controls();
    return {session, refresh, close() { alive = false; ++version; ++rowsVersion; ++detailVersion; root.replaceChildren(); }};
  }
  return {mount, hexDump, treeHtml};
})();
