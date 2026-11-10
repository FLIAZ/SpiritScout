"use strict";
/* JS 情报已并入资产黑板；这里只保留资产详情中的受审计秘密查看器。 */
const AssetSecretViewer = (() => {
  let revealGeneration = 0, dialog = null;

  function close() {
    revealGeneration++;
    if (dialog) { dialog.remove(); dialog = null; }
  }

  async function reveal(reference, target) {
    close();
    const token = revealGeneration, origin = S.cur;
    const view = document.createElement("dialog");
    dialog = view;
    view.innerHTML = '<h3>会话秘密明文</h3><p>本次查看会记录审计。关闭后清除显示；不要复制到普通报告。</p><pre role="status">正在读取…</pre><button type="button" class="btn">关闭并清除</button>';
    view.querySelector("button").onclick = close;
    view.addEventListener("close", close, {once:true});
    document.body.append(view);
    view.showModal();
    try {
      const result = await api(`/api/sessions/${encodeURIComponent(origin)}/secrets/${encodeURIComponent(reference)}/reveal`, {
        method:"POST", body:JSON.stringify({confirm:true, target}),
      });
      if (token === revealGeneration && S.cur === origin && dialog === view) {
        view.querySelector("pre").textContent = result.value;
      }
    } catch (error) {
      if (token === revealGeneration && dialog === view) view.querySelector("pre").textContent = error.message;
    }
  }

  function bind(root) {
    root?.querySelectorAll("[data-secret-ref]").forEach(button => {
      button.onclick = () => reveal(button.dataset.secretRef, button.dataset.secretTarget);
    });
  }

  return {bind, close};
})();
