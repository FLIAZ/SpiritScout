"use strict";
const ConsoleNotify = (() => {
  const SEEN_KEY = "ss.notify.seen", READ_KEY = "ss.notify.read", ENABLED_KEY = "ss.notify.enabled";
  const SYSTEM_KEY = "ss.notify.system", MAX_SEEN = 400;
  function create({fetchItems, storage, notifyCtor, doc, win, toastFn, isCurrent, openSession, onUpdate = () => {}}) {
    const baseTitle = doc.title;
    let snapshot = {items: [], total: 0}, flight = null, error = "", systemChoice = null;
    const read = (key, fallback) => { try { return storage?.getItem(key) ?? fallback; } catch { return fallback; } };
    const write = (key, value) => { try { storage?.setItem(key, value); } catch {} };
    function loadIds(key) {
      try { const values = JSON.parse(read(key, "[]")); return Array.isArray(values) ? values.filter(value => typeof value === "string").slice(-MAX_SEEN) : []; }
      catch { return []; }
    }
    let seen = new Set(loadIds(SEEN_KEY)), readIds = new Set(loadIds(READ_KEY));
    const enabled = () => read(ENABLED_KEY, "on") !== "off";
    const permission = () => notifyCtor?.permission || "unsupported";
    const systemEnabled = () => read(SYSTEM_KEY, systemChoice ?? (enabled() ? "on" : "off")) !== "off";
    const items = () => (snapshot.items || []).filter(item => item && typeof item.id === "string");
    function markRead(ids = items().map(item => item.id)) {
      for (const id of loadIds(READ_KEY)) readIds.add(id);
      for (const id of ids) readIds.add(id);
      readIds = new Set([...readIds].slice(-MAX_SEEN));
      write(READ_KEY, JSON.stringify([...readIds]));
    }
    function unreadItems() {
      const known = new Set([...readIds, ...loadIds(READ_KEY)]);
      return items().filter(item => !known.has(item.id) && !(doc.visibilityState === "visible" && isCurrent(item.session_id)));
    }
    const unread = () => unreadItems().length;
    function raise(item) {
      const body = `${item.message || I18n.t("点击查看会话")}${item.session_title ? `（${item.session_title}）` : ""}`;
      if (systemEnabled() && permission() === "granted") {
        try {
          const note = new notifyCtor(item.title || I18n.t("灵枢通知"), {body, tag: item.id});
          note.onclick = () => { win.focus(); markRead([item.id]); onUpdate(); if (item.session_id) openSession(item.session_id); note.close?.(); };
          return true;
        } catch {}
      }
      toastFn(`${item.title || I18n.t("灵枢通知")}：${body}`);
      doc.title = I18n.t`有新提醒 · ${baseTitle}`;
      return false;
    }
    async function checkOnce() {
      try { snapshot = await fetchItems(); error = ""; }
      catch { error = I18n.t("提醒读取失败，请重试"); onUpdate(); return []; }
      for (const id of loadIds(SEEN_KEY)) seen.add(id);
      const fresh = items().filter(item => !seen.has(item.id));
      let raised = 0, overflow = 0;
      for (const item of fresh) {
        seen.add(item.id);
        if (!enabled() || (doc.visibilityState === "visible" && isCurrent(item.session_id))) continue;
        if (raised >= 3) { overflow++; continue; }
        raise(item); raised++;
      }
      if (overflow) raise({id: "notify-burst", title: I18n.t("还有新提醒"), message: I18n.t`另有 ${overflow} 条，点击铃铛查看。`});
      if (doc.visibilityState === "visible") {
        const currentIds = items().filter(item => isCurrent(item.session_id)).map(item => item.id);
        if (currentIds.length) markRead(currentIds);
      }
      seen = new Set([...seen].slice(-MAX_SEEN));
      if (fresh.length) write(SEEN_KEY, JSON.stringify([...seen]));
      onUpdate();
      return fresh;
    }
    return {
      check() { if (!flight) flight = checkOnce().finally(() => { flight = null; }); return flight; },
      enabled, permission, systemEnabled, unread, unreadItems, markRead, snapshot: () => snapshot, error: () => error,
      setEnabled(on) { write(ENABLED_KEY, on ? "on" : "off"); },
      setSystemEnabled(on) {
        systemChoice = on ? "on" : "off";
        write(SYSTEM_KEY, systemChoice);
        if (on) write(ENABLED_KEY, "on");
        onUpdate();
      },
      async requestPermission() { if (notifyCtor?.requestPermission) await notifyCtor.requestPermission(); return permission(); },
      clearFlash() { doc.title = baseTitle; },
      clearSeen() { seen.clear(); write(SEEN_KEY, "[]"); },
      selfTest() { return raise({id: "notify-test", title: I18n.t("通知测试"), message: I18n.t("解题完成或需要你回答时，会这样提醒。")}); },
    };
  }
  let instance;
  const panel = () => $("#notify-panel");
  function positionPanel() {
    if (panel()?.hidden !== false) return;
    const anchor = $("#btn-notify").getBoundingClientRect(), width = Math.min(360, window.innerWidth - 24);
    Object.assign(panel().style, {width: `${width}px`, left: `${Math.max(12, Math.min(anchor.left, window.innerWidth - width - 12))}px`,
      top: `${Math.max(12, Math.min(anchor.bottom + 8, window.innerHeight - panel().offsetHeight - 12))}px`});
  }
  function syncSystemButton() {
    const ctl = instance, button = $("#btn-system-notify");
    if (!ctl || !button) return;
    const on = ctl.systemEnabled() && ctl.permission() === "granted";
    const labels = {denied: I18n.t("系统通知已被浏览器拒绝，可在站点设置中修改"), unsupported: I18n.t("当前浏览器不支持系统通知")};
    button.setAttribute("aria-checked", String(on));
    button.disabled = ctl.permission() === "unsupported";
    button.title = labels[ctl.permission()] || I18n.t`系统通知：${on ? I18n.t("已开启，点击关闭") : I18n.t("已关闭，点击开启")}`;
    button.setAttribute("aria-label", button.title);
  }
  function paint() {
    if (!instance) return;
    const root = panel(), bell = $("#btn-notify");
    const count = instance.unread();
    if (bell) {
      bell.querySelector(".notify-dot").hidden = !count;
      bell.setAttribute("aria-label", count ? I18n.t`通知，${count} 条未读提醒` : I18n.t("通知"));
    }
    const toggle = $("#sidebar-sessions-toggle");
    if (toggle) {
      toggle.dataset.notifyUnread = count ? "true" : "false";
      toggle.setAttribute("aria-description", count ? I18n.t("会话列表，有未读提醒") : "");
    }
    syncSystemButton();
    if (root) root.querySelector("[data-notify-read]").disabled = !count;
    if (!root || root.hidden) return;
    const data = instance.snapshot(), rows = instance.unreadItems(), list = root.querySelector("[data-notify-items]");
    root.querySelector("[data-notify-status]").textContent = instance.error() || (data.truncated ? I18n.t("仅展示最近记录中的未读提醒") : "");
    const signature = JSON.stringify([rows, instance.error()]);
    if (list.dataset.snapshot !== signature) {
      const focused = list.contains(document.activeElement) ? document.activeElement.dataset.notifyId : null;
      list.dataset.snapshot = signature;
      list.innerHTML = rows.map(item => `<button type="button" class="notify-item${item.type === "question_pending" ? " pending" : ""}" data-notify-id="${esc(item.id)}" data-session="${esc(item.session_id)}"><strong>${esc(item.title)}</strong>${item.session_title ? `<span class="notify-session">${esc(item.session_title)}</span>` : ""}<span>${esc(item.message || "")}</span></button>`).join("") || (instance.error() ? "" : I18n.html('<p class="notify-empty">暂无未读提醒</p>'));
      list.querySelectorAll("[data-session]").forEach(button => {
        button.onclick = () => { instance.markRead([button.dataset.notifyId]); close(); paint(); openSession(button.dataset.session); };
        if (focused && button.dataset.notifyId === focused) button.focus();
      });
    }
    positionPanel();
  }
  function close(focus = false) {
    if (panel()) panel().hidden = true;
    $("#btn-notify")?.setAttribute("aria-expanded", "false");
    if (focus) $("#btn-notify")?.focus();
  }
  async function open() {
    const ctl = init(), root = panel();
    if (!root) return;
    root.hidden = false;
    $("#btn-notify").setAttribute("aria-expanded", "true");
    paint();
    root.querySelector("[data-notify-status]").textContent = I18n.t("正在读取…");
    root.querySelector("[data-notify-close]").focus();
    await ctl.check();
  }
  function init() {
    if (instance) return instance;
    let storage = null;
    try { storage = window.localStorage; } catch {}
    instance = create({fetchItems: () => api("/api/notifications"), storage,
      notifyCtor: window.Notification, doc: document, win: window, toastFn: toast,
      isCurrent: id => S.view === "session" && S.cur === id, openSession, onUpdate: paint});
    const bell = $("#btn-notify"), root = panel(), system = $("#btn-system-notify");
    if (bell) bell.onclick = () => root.hidden ? open() : close(true);
    if (root) {
      root.querySelector("[data-notify-close]").onclick = () => close(true);
      root.querySelector("[data-notify-read]").onclick = () => {
        instance.markRead(); paint();
        root.querySelector("[data-notify-close]").focus();
      };
      root.querySelector("[data-notify-refresh]").onclick = () => instance.check();
    }
    if (system) system.onclick = async () => {
      if (instance.systemEnabled() && instance.permission() === "granted") { instance.setSystemEnabled(false); return; }
      system.disabled = true;
      try {
        const permission = instance.permission() === "default" ? await instance.requestPermission() : instance.permission();
        instance.setSystemEnabled(permission === "granted");
        if (permission !== "granted") toast(I18n.t("系统通知未开启；仍可通过铃铛查看提醒"), true);
      } catch { toast(I18n.t("系统通知权限申请失败"), true); }
      finally { system.disabled = false; syncSystemButton(); }
    };
    document.addEventListener("click", event => {
      if (panel()?.hidden === false && !panel().contains(event.target) && !bell.contains(event.target)) close();
    });
    document.addEventListener("keydown", event => {
      if (event.key === "Escape" && panel()?.hidden === false) { event.preventDefault(); close(true); }
    });
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") instance.clearFlash();
    });
    window.addEventListener("focus", () => { instance.clearFlash(); paint(); });
    window.addEventListener("resize", positionPanel);
    paint();
    return instance;
  }
  return {create, init, open, close, check: () => init().check()};
})();
