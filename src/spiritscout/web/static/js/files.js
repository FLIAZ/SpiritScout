"use strict";
/* 会话文件卡片：像聊天软件一样收发文件。
 * - 卡片渲染（按扩展名给图标，预览/下载按钮）
 * - 在线预览：Markdown 渲染成文档、文本/代码原样、图片直接显示
 * - 下载：优先 File System Access API 让用户选目录，退回普通下载
 * - 发送到会话：把产出目录里的文件投递成会话内的文件消息 */
/* global mdRender, toast, esc, S */

const FileCards = (() => {
  const TEXT_EXT = new Set(["txt", "log", "json", "csv", "xml", "yml", "yaml",
    "ini", "conf", "cfg", "py", "js", "ts", "html", "css", "c", "h", "cpp",
    "java", "go", "rs", "sh", "bash", "sql", "toml", "env", "list", "dict"]);
  const IMG_EXT = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"]);
  // 预览超过该体积直接提示下载，避免把大文件整段读进会话页
  const PREVIEW_LIMIT = 4 * 1024 * 1024;

  const ext = (name) => {
    const i = String(name || "").lastIndexOf(".");
    return i > 0 ? String(name).slice(i + 1).toLowerCase() : "";
  };

  const ICONS = {
    md: '<path d="M3 5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/><path d="M7 15l3-4 2 2.5L13.5 11l3.5 4"/><path d="M7 8h4"/>',
    img: '<rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="8.5" cy="10" r="1.5"/><path d="M21 15l-5-5-9 9"/>',
    zip: '<path d="M4 4h16v16H4z"/><path d="M12 4v4m0 2v2m0 2v2"/>',
    code: '<path d="M8 6l-5 6 5 6"/><path d="M16 6l5 6-5 6"/><path d="M13 4l-2 16"/>',
    file: '<path d="M13 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9z"/><path d="M13 3v6h6"/>',
  };

  const iconSvg = (name) => {
    const e = ext(name);
    const key = e === "md" ? "md" : IMG_EXT.has(e) ? "img"
      : ["zip", "gz", "tar", "7z", "rar"].includes(e) ? "zip"
        : ["py", "js", "ts", "sh", "c", "cpp", "java", "go", "rs", "html", "css", "sql"].includes(e) ? "code" : "file";
    return `<svg class="fc-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[key]}</svg>`;
  };

  const sizeText = (n) => {
    if (n === null || n === undefined || Number(n) < 0) return "";
    if (Number(n) < 1024) return `${n} B`;
    const units = ["KB", "MB", "GB"];
    let v = Number(n) / 1024, i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
    return `${v >= 10 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
  };

  const extLabel = (name) => {
    const e = ext(name);
    if (!e) return "文件";
    if (e === "md") return "Markdown";
    if (IMG_EXT.has(e)) return "图片";
    return e.toUpperCase();
  };

  const isPreviewable = (name) => {
    const e = ext(name);
    return e === "md" || TEXT_EXT.has(e) || IMG_EXT.has(e);
  };

  const fileUrl = (session, rel) =>
    `/api/sessions/${encodeURIComponent(session)}/artifacts/download?path=${encodeURIComponent(rel)}`;

  /* ---- 卡片（气泡内的文件消息） ---- */
  function card({ rel, name, size, side }) {
    const fname = name || String(rel || "").split("/").pop() || "文件";
    const ops = rel ? [
      isPreviewable(fname)
        ? `<button type="button" class="fc-btn" data-file-view>预览</button>` : "",
      `<button type="button" class="fc-btn" data-file-dl>下载</button>`,
    ].join("") : "";
    return `<div class="filecard${side === "user" ? " fc-user" : ""}" data-file-rel="${esc(rel || "")}"
      data-file-name="${esc(fname)}" data-file-size="${size ?? ""}">
      ${iconSvg(fname)}
      <span class="fc-main"><b class="fc-name" title="${esc(fname)}">${esc(fname)}</b>
        <small class="fc-meta">${[sizeText(size), extLabel(fname)].filter(Boolean).join(" · ")}</small></span>
      <span class="fc-ops">${ops}</span>
    </div>`;
  }

  /* ---- 下载：能选目录就选目录（Chromium），否则普通下载 ---- */
  async function saveFile(url, name = "") {
    let handle = null, writer = null;
    let reportDownload = false;
    try { reportDownload = new URL(url, window.location?.href || "http://localhost").pathname.endsWith("/report/download"); } catch {}
    if (reportDownload) {
      name = name || "报告.md";
      if (!/\.md$/i.test(name)) name += ".md";
    }
    try {
      if (window.showSaveFilePicker) {
        let suggestedName = name;
        if (!suggestedName) {
          try { suggestedName = new URL(url, window.location?.href || "http://localhost").searchParams.get("path")?.split("/").pop() || ""; } catch {}
        }
        try {
          handle = await window.showSaveFilePicker({ suggestedName: suggestedName || "下载文件",
            ...(/\.md$/i.test(suggestedName) ? {types: [{description: "Markdown 文档", accept: {"text/markdown": [".md"]}}]} : {}) });
        } catch (e) {
          if (e && e.name === "AbortError") return;
        }
      }
      const resp = await fetch(url);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      if (handle) {
        writer = await handle.createWritable();
        if (resp.body) await resp.body.pipeTo(writer);
        else { await writer.write(await resp.blob()); await writer.close(); }
        writer = null;
        toast(`已保存 ${handle.name || name || "下载文件"}`);
        return;
      }
      const blob = await resp.blob();
      // 锚点没带文件名时，从响应 Content-Disposition 里取
      let fname = name;
      if (!fname) {
        const cd = resp.headers.get("Content-Disposition") || "";
        const m = cd.match(/filename\*?=(?:UTF-8'')?"?([^";]+)/i);
        if (m) {
          try { fname = decodeURIComponent(m[1]); } catch { fname = m[1]; }
        }
      }
      fname = fname || "下载文件";
      const a = document.createElement("a");
      const objUrl = URL.createObjectURL(blob);
      a.href = objUrl;
      a.download = fname;
      a.click();
      setTimeout(() => URL.revokeObjectURL(objUrl), 8000);
    } catch (e) {
      if (writer) { try { await writer.abort(); } catch {} }
      toast(`下载失败：${e && e.message ? e.message : e}`, true);
    }
  }

  /* ---- 在线预览 ---- */
  let viewerObjUrl = null, viewerController = null, viewerVersion = 0;

  async function openViewer(session, rel, name, size) {
    const dlg = document.getElementById("file-dlg");
    if (!dlg) return;
    const version = ++viewerVersion;
    viewerController?.abort(); viewerController = null;
    if (viewerObjUrl) { URL.revokeObjectURL(viewerObjUrl); viewerObjUrl = null; }
    const current = () => version === viewerVersion && dlg.open;
    const fname = name || String(rel).split("/").pop() || "文件";
    const e = ext(fname);
    document.getElementById("fd-name").textContent = fname;
    document.getElementById("fd-meta").textContent =
      [sizeText(size), extLabel(fname)].filter(Boolean).join(" · ");
    document.getElementById("fd-dl").onclick = () => saveFile(fileUrl(session, rel), fname);
    const body = document.getElementById("fd-body");
    const render = async () => {
      body.innerHTML = `<div class="fd-loading">正在读取…</div>`;
      viewerController = new AbortController();
      const signal = viewerController.signal;
      try {
        const resp = await fetch(fileUrl(session, rel), {signal});
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        if (IMG_EXT.has(e)) {
          const blob = await resp.blob();
          if (!current()) return;
          viewerObjUrl = URL.createObjectURL(blob);
          body.innerHTML = `<img class="fd-img" src="${viewerObjUrl}" alt="${esc(fname)}">`;
        } else {
          const text = await resp.text();
          if (!current()) return;
          body.innerHTML = e === "md"
            ? `<div class="fd-md md">${mdRender(text)}</div>`
            : `<pre class="fd-pre">${esc(text)}</pre>`;
        }
      } catch (err) {
        if (!current() || err.name === "AbortError") return;
        body.innerHTML = `<div class="fd-tip">读取失败：${esc(err && err.message ? err.message : String(err))}</div>`;
      } finally {
        if (version === viewerVersion) viewerController = null;
      }
    };
    if (!dlg.open) dlg.showModal();
    if (!isPreviewable(fname)) {
      body.innerHTML = `<div class="fd-tip">该类型不支持在线预览，请下载后查看。</div>`;
    } else if (Number(size) > PREVIEW_LIMIT) {
      body.innerHTML = `<div class="fd-tip">文件较大（${sizeText(size)}），请下载后查看。</div>`;
    } else {
      await render();
    }
  }

  /* ---- 发送到会话（产出/报告/附件目录内的文件） ---- */
  async function share(session, rel, note = "") {
    const resp = await fetch(`/api/sessions/${encodeURIComponent(session)}/files/share`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: rel, note }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
  }

  /* ---- 事件委托：卡片按钮、既有下载锚点统一走目录选择 ---- */
  document.addEventListener("click", async (event) => {
    const btn = event.target.closest("[data-file-view],[data-file-dl]");
    if (btn) {
      const host = btn.closest("[data-file-rel]");
      if (!host || !host.dataset.fileRel) return;
      const session = S.cur;
      const { fileRel: rel, fileName: name, fileSize: size } = host.dataset;
      if (btn.hasAttribute("data-file-view")) {
        await openViewer(session, rel, name, size === "" ? undefined : Number(size));
      } else {
        await saveFile(fileUrl(session, rel), name);
      }
      return;
    }
    // 报告/档案页里已有的 a[download]：换成可选目录的保存流程
    const anchor = event.target.closest('a[download][href^="/api/sessions/"]');
    if (anchor && window.showSaveFilePicker) {
      event.preventDefault();
      await saveFile(anchor.getAttribute("href"), anchor.getAttribute("download") || "");
    }
  });

  // 关闭按钮、点遮罩关闭；关闭时释放图片 blob 并清空内容
  const dlgEl = document.getElementById("file-dlg");
  if (dlgEl) {
    const closeBtn = document.getElementById("fd-close");
    if (closeBtn) closeBtn.addEventListener("click", () => dlgEl.close());
    dlgEl.addEventListener("click", (event) => {
      if (event.target === dlgEl) dlgEl.close();
    });
    dlgEl.addEventListener("close", () => {
      if (dlgEl.open) return;
      viewerVersion++; viewerController?.abort(); viewerController = null;
      if (viewerObjUrl) { URL.revokeObjectURL(viewerObjUrl); viewerObjUrl = null; }
      const body = document.getElementById("fd-body");
      if (body) body.innerHTML = "";
    });
  }

  return { card, saveFile, openViewer, share, isPreviewable, fileUrl, sizeText };
})();
