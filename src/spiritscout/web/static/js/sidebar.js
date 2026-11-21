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
  let storage = null, mobileOpen = false, view = "home", contextOpen = false, readingMenuCollapsed = true;
  let preferences = {menuCollapsed: false, sessionsCollapsed: false};
  const normalize = value => ({menuCollapsed: value?.menuCollapsed === true,
    sessionsCollapsed: value?.sessionsCollapsed === true});
  try {
    storage = window.localStorage;
    preferences = normalize(JSON.parse(storage.getItem(key)));
  } catch {}

  function render() {
    const sessionsOpen = media.matches ? mobileOpen : view === "home" ? !preferences.sessionsCollapsed : contextOpen;
    const menuCollapsed = view === "session" ? readingMenuCollapsed : preferences.menuCollapsed;
    root.dataset.menuCollapsed = String(menuCollapsed);
    root.dataset.sessionsCollapsed = String(!sessionsOpen);
    root.dataset.view = view;
    // 不移除布局节点，让收起过程可过渡；交互和焦点立即服从新状态。
    if (!sessionsOpen && panel.contains(document.activeElement)) sessionsButton.focus();
    panel.inert = !sessionsOpen;
    const menuLabel = menuCollapsed ? I18n.t("展开菜单标题") : I18n.t("收起菜单标题");
    menuButton.title = menuLabel;
    menuButton.setAttribute("aria-label", menuLabel);
    menuButton.setAttribute("aria-expanded", String(!menuCollapsed));
    const sessionsLabel = sessionsOpen ? I18n.t("收起会话列表") : I18n.t("展开会话列表");
    sessionsButton.title = sessionsLabel;
    sessionsButton.setAttribute("aria-label", sessionsLabel);
    sessionsButton.setAttribute("aria-expanded", String(sessionsOpen));
    sessionsButton.querySelector(".menu-label").textContent = sessionsLabel;
  }
  function save() {
    try { storage?.setItem(key, JSON.stringify(preferences)); } catch {}
    render();
  }
  menuButton.onclick = () => {
    if (view === "session") { readingMenuCollapsed = !readingMenuCollapsed; render(); return; }
    preferences.menuCollapsed = !preferences.menuCollapsed;
    save();
  };
  sessionsButton.onclick = () => {
    if (media.matches) { mobileOpen = !mobileOpen; render(); }
    else if (view === "home") { preferences.sessionsCollapsed = !preferences.sessionsCollapsed; save(); }
    else { contextOpen = !contextOpen; render(); }
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
  const nav = document.getElementById("primary-nav");
  if (nav && typeof ResizeObserver !== "undefined") {
    let density = 1, frame = null;
    function fitNavigation() {
      frame = null;
      const style = getComputedStyle(nav);
      const px = value => parseFloat(value) || 0;
      const space = name => px(style.getPropertyValue(name));
      const groups = [...nav.children].filter(group => group.getClientRects().length);
      const gaps = Math.max(0, groups.length - 1);
      let used = px(style.paddingTop) + px(style.paddingBottom) + px(style.rowGap) * gaps;
      let adjustable = px(style.paddingBottom) - 6 + px(style.rowGap) * gaps;
      let maximum = space("--nav-tail-space") + space("--nav-group-space") * gaps;
      for (const [index, group] of groups.entries()) {
        const groupStyle = getComputedStyle(group);
        const children = [...group.children].filter(child => child.getClientRects().length);
        const itemGaps = Math.max(0, children.length - 1);
        used += group.getBoundingClientRect().height;
        adjustable += px(groupStyle.rowGap) * itemGaps + px(groupStyle.paddingTop);
        maximum += space("--nav-item-space") * itemGaps;
        if (index) maximum += space("--nav-separator-space");
        const label = children.find(child => child.classList.contains("nav-section"));
        if (label) {
          const labelStyle = getComputedStyle(label);
          adjustable += px(labelStyle.paddingBottom) + px(labelStyle.marginBottom);
          maximum += space("--nav-label-space") + space("--nav-label-correction");
        }
      }
      // 从实际内容中剥离可伸缩留白；按钮高度不变，过矮窗口仍可滚动。
      const next = maximum > 0 ? Math.max(0, Math.min(1,
        (nav.clientHeight - (used - adjustable) - 1) / maximum)) : 1;
      if (Math.abs(next - density) < .001) return;
      density = next;
      nav.style.setProperty("--nav-density", String(density));
    }
    const observer = new ResizeObserver(() => {
      if (frame === null) frame = requestAnimationFrame(fitNavigation);
    });
    observer.observe(nav);
    for (const group of nav.children) observer.observe(group);
  }
  function navigate(nextView) {
    if (view !== nextView) { contextOpen = false; mobileOpen = false; readingMenuCollapsed = true; }
    view = nextView;
    render();
  }
  return {closeOnMobile, navigate};
})();
