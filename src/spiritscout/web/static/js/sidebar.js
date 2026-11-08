"use strict";
/* 两种折叠分别保存；切换只改变布局，不重建会话列表或中断当前页面。 */
const ConsoleSidebar = (() => {
  const key = "ss-sidebar-layout";
  const root = document.documentElement;
  const panel = document.getElementById("session-column");
  const menuButton = document.getElementById("sidebar-menu-toggle");
  const sessionsButton = document.getElementById("sidebar-sessions-toggle");
  const mobileClose = document.getElementById("sidebar-mobile-close");
  const media = window.matchMedia("(max-width: 860px)");
  let storage = null, mobileOpen = false;
  let preferences = {menuCollapsed: false, sessionsCollapsed: false};
  const normalize = value => ({menuCollapsed: value?.menuCollapsed === true,
    sessionsCollapsed: value?.sessionsCollapsed === true});
  try {
    storage = window.localStorage;
    preferences = normalize(JSON.parse(storage.getItem(key)));
  } catch {}

  function render() {
    const sessionsOpen = media.matches ? mobileOpen : !preferences.sessionsCollapsed;
    root.dataset.menuCollapsed = String(preferences.menuCollapsed);
    root.dataset.sessionsCollapsed = String(preferences.sessionsCollapsed);
    panel.hidden = !sessionsOpen;
    const menuLabel = preferences.menuCollapsed ? I18n.t("展开菜单标题") : I18n.t("收起菜单标题");
    menuButton.title = menuLabel;
    menuButton.setAttribute("aria-label", menuLabel);
    menuButton.setAttribute("aria-expanded", String(!preferences.menuCollapsed));
    const sessionsLabel = sessionsOpen ? I18n.t("收起会话列表") : I18n.t("展开会话列表");
    sessionsButton.title = sessionsLabel;
    sessionsButton.setAttribute("aria-label", sessionsLabel);
    sessionsButton.setAttribute("aria-expanded", String(sessionsOpen));
    sessionsButton.querySelector(".menu-label").textContent = sessionsLabel;
    if (!sessionsOpen && panel.contains(document.activeElement)) sessionsButton.focus();
  }
  function save() {
    try { storage?.setItem(key, JSON.stringify(preferences)); } catch {}
    render();
  }
  menuButton.onclick = () => {
    preferences.menuCollapsed = !preferences.menuCollapsed;
    save();
  };
  sessionsButton.onclick = () => {
    if (media.matches) { mobileOpen = !mobileOpen; render(); }
    else { preferences.sessionsCollapsed = !preferences.sessionsCollapsed; save(); }
  };
  function closeOnMobile() {
    if (media.matches) { mobileOpen = false; render(); }
  }
  mobileClose.onclick = () => { closeOnMobile(); sessionsButton.focus(); };
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && media.matches && mobileOpen && !document.querySelector("dialog[open], :popover-open")) {
      closeOnMobile(); sessionsButton.focus();
    }
  });
  media.addEventListener("change", () => { mobileOpen = false; render(); });
  window.addEventListener("storage", event => {
    if (event.storageArea !== storage || (event.key !== key && event.key !== null)) return;
    try { preferences = normalize(JSON.parse(event.newValue)); } catch { return; }
    render();
  });
  render();
  return {closeOnMobile};
})();
