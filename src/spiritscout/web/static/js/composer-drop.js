"use strict";
/* 拖放只加入所在页面的当前草稿，仍由用户点击发送；普通文本拖动保持原行为。 */
const ComposerDrop = (() => {
  const contexts = [
    {view:"home", zone:document.getElementById("view-home"), hint:document.getElementById("home-drop-status"),
      files:() => S.homeFiles, chips:"#home-files", available:() => !document.getElementById("c-go").disabled},
    {view:"session", zone:document.getElementById("view-session"), hint:document.getElementById("comp-drop-status"),
      files:() => S.compFiles, chips:"#comp-files", available:() => !!S.cur && !sessionMessagePending.has(S.cur)},
  ];
  const busy = new Set();
  const hasFiles = event => Array.from(event.dataTransfer?.types || []).includes("Files");
  const target = event => !document.querySelector("dialog[open]") && contexts.find(ctx =>
    S.view === ctx.view && ctx.zone.contains(event.target));
  const reset = () => contexts.forEach(ctx => { ctx.zone.classList.remove("file-drag"); ctx.hint.hidden = true; });
  document.addEventListener("dragover", event => {
    if (!hasFiles(event)) return;
    event.preventDefault(); reset();
    const ctx = target(event), accept = ctx && ctx.available() && !busy.has(ctx);
    event.dataTransfer.dropEffect = accept ? "copy" : "none";
    if (accept) { ctx.zone.classList.add("file-drag"); ctx.hint.hidden = false; }
  });
  document.addEventListener("dragleave", event => {
    if (!contexts.some(ctx => ctx.zone.contains(event.relatedTarget))) reset();
  });
  document.addEventListener("drop", async event => {
    if (!hasFiles(event)) return;
    event.preventDefault(); reset();
    const ctx = target(event);
    if (!ctx) return;
    if (!ctx.available() || busy.has(ctx)) { toast(I18n.t("正在处理附件或发送任务，请稍后再添加"), true); return; }
    const draft = ctx.files(), id = S.cur;
    busy.add(ctx);
    try {
      const files = await ComposerFolders.collect(event.dataTransfer);
      if (!files.length) return;
      // 目录读取为异步：切页、切会话、发送后，不能把文件混入其他草稿。
      if (S.view !== ctx.view || (ctx.view === "session" && S.cur !== id) || ctx.files() !== draft || !ctx.available()) {
        toast(I18n.t("页面或草稿已变化，请在目标会话重新拖入附件"), true); return;
      }
      if (draft.length + files.length > 20) { toast(I18n.t("一次最多携带 20 个附件（文件夹计为一个附件）"), true); return; }
      if (files.some(file => (!file.directory && !file.size) || file.size > 50 * 1024 * 1024)) {
        toast(I18n.t("附件不能为空，且单个文件不能超过 50MB"), true); return;
      }
      draft.push(...files);
      renderFileChips(ctx.chips, draft);
    } catch (error) { toast(I18n.t`添加附件失败：${error.message || I18n.t("无法读取文件")}`, true); }
    finally { busy.delete(ctx); }
  });
  document.addEventListener("dragend", reset);
  window.addEventListener("blur", reset);
  return {pending:view => contexts.some(ctx => ctx.view === view && busy.has(ctx))};
})();
