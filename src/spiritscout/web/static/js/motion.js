"use strict";
/* CSS 承担页面动效；这里仅管理本机偏好、系统变化和按需预览，不启动轮询。 */
(() => {
  const key = "ss-motion", labels = {auto: I18n.t("跟随系统"), soft: I18n.t("柔和"), quick: I18n.t("快速"), off: I18n.t("关闭")};
  const root = document.documentElement, dialog = document.getElementById("motion-dlg");
  const button = document.getElementById("btn-motion"), sample = document.getElementById("motion-sample");
  const media = window.matchMedia("(prefers-reduced-motion: reduce)");
  const inputs = [...dialog.querySelectorAll('input[name="motion"]')];
  let preference = "auto", storage = null, saved = true, preview = null;
  const normalize = value => Object.hasOwn(labels, value) ? value : "auto";
  try { storage = window.localStorage; preference = normalize(storage.getItem(key)); }
  catch { saved = false; }

  function replay() {
    preview?.cancel(); preview = null;
    if (!dialog.open || preference === "off" || media.matches || document.hidden || !sample.animate) return;
    const style = getComputedStyle(root);
    preview = sample.animate([
      {opacity: .35, transform: `translateY(${style.getPropertyValue("--motion-distance").trim()})`},
      {opacity: 1, transform: "none"},
    ], {duration: parseFloat(style.getPropertyValue("--motion-enter")),
      easing: style.getPropertyValue("--motion-ease").trim()});
  }
  function render() {
    root.dataset.motion = preference;
    for (const input of inputs) input.checked = input.value === preference;
    document.getElementById("motion-current").textContent = labels[preference];
    document.getElementById("motion-system-note").hidden = !media.matches;
    document.getElementById("motion-save-note").textContent = saved
      ? I18n.t("仅在当前浏览器保存，随时可以调整。") : I18n.t("浏览器暂时无法保存设置，本次打开期间仍会生效。");
    button.title = I18n.t`界面动效：${labels[preference]}${media.matches ? I18n.t("（已减少动态效果）") : ""}`;
    sample.textContent = media.matches || preference === "off" ? I18n.t("即时呈现")
      : preference === "quick" ? I18n.t("短促利落") : preference === "soft" ? I18n.t("轻缓舒展") : I18n.t("轻盈过渡");
    replay();
  }
  function choose(value) {
    preference = normalize(value);
    try { storage?.setItem(key, preference); saved = !!storage; }
    catch { saved = false; }
    render();
  }
  button.onclick = () => {
    if (!dialog.open) dialog.showModal();
    button.setAttribute("aria-expanded", "true");
    inputs.find(input => input.checked)?.focus({preventScroll: true});
    replay();
  };
  document.getElementById("motion-close").onclick = () => dialog.close();
  document.getElementById("motion-replay").onclick = replay;
  for (const input of inputs) input.addEventListener("change", () => { if (input.checked) choose(input.value); });
  dialog.addEventListener("close", () => {
    preview?.cancel(); preview = null;
    button.setAttribute("aria-expanded", "false");
  });
  media.addEventListener("change", render);
  window.addEventListener("storage", event => {
    if (event.storageArea !== storage || (event.key !== key && event.key !== null)) return;
    preference = normalize(event.newValue); saved = true; render();
  });
  function visibility() {
    root.toggleAttribute("data-page-hidden", document.hidden);
    if (document.hidden) { preview?.cancel(); preview = null; }
  }
  document.addEventListener("visibilitychange", visibility);
  visibility(); render();
})();
