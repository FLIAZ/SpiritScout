"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync("src/spiritscout/web/static/js/notify.js", "utf8");
const {create} = vm.runInNewContext(source + "\nConsoleNotify", require('./helpers/i18n').context({}));

async function main() {
  const values = new Map(), sent = [], toasts = [], opened = [];
  const storage = {getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value)};
  let permissionRequests = 0, current = false, fail = false;
  let items = [{id: "one", session_id: "session-one", title: "等待回答", type: "question_pending"}];
  class Notification {
    static permission = "granted";
    static async requestPermission() { permissionRequests++; return Notification.permission; }
    constructor(title, options) { this.options = options; sent.push(this); }
    close() {}
  }
  const options = {fetchItems: async () => { if (fail) throw new Error("fixture offline"); return {items}; },
    storage, notifyCtor: Notification, doc: {title: "fixture", visibilityState: "visible"},
    win: {focus() {}}, toastFn: text => toasts.push(text), isCurrent: () => current,
    openSession: id => opened.push(id)};
  const ctl = create(options);
  ctl.setSystemEnabled(false);
  await ctl.check();
  assert.equal(sent.length, 0, "关闭系统通知后不弹桌面通知");
  assert.equal(toasts.length, 1, "页面提醒继续工作");
  assert.equal(ctl.unread(), 1, "已经投递的提醒仍可在铃铛中未读");
  assert.equal(ctl.unreadItems()[0].id, "one");
  assert.equal(permissionRequests, 0, "轮询不申请系统通知权限");
  ctl.markRead();
  assert.equal(ctl.unread(), 0);
  assert.equal(ctl.unreadItems().length, 0, "已读提醒从显示列表自动移除");
  assert.equal(ctl.snapshot().items.length, 1, "已读不会删除待回答提醒");
  await ctl.check();
  assert.equal(toasts.length, 1, "打开提醒后不重复投递");
  const restored = create(options);
  await restored.check();
  assert.equal(restored.unread(), 0, "已读状态跨刷新保留");
  assert.equal(restored.unreadItems().length, 0, "刷新后不重新显示已读提醒");
  assert.equal(restored.systemEnabled(), false, "系统通知选择跨刷新保留");

  ctl.setSystemEnabled(true);
  items = [...items, {id: "two", session_id: "session-two", title: "已完成"}];
  await ctl.check();
  assert.equal(sent.length, 1);
  assert.equal(ctl.unread(), 1);
  sent[0].onclick();
  assert.deepEqual(opened, ["session-two"]);
  assert.equal(ctl.unread(), 0, "点击系统通知会标记相应提醒已读");

  current = true;
  items = [...items, {id: "three", session_id: "session-three", title: "已完成"}];
  await ctl.check();
  current = false;
  assert.equal(ctl.unread(), 0, "当前会话中已看到的提醒不再次点亮铃铛");
  assert.equal(sent.length, 1);
  fail = true;
  await ctl.check();
  assert.match(ctl.error(), /读取失败/);
  assert.equal(ctl.snapshot().items.length, 3, "读取失败保留上次快照");

  const blocked = create({...options, storage: {getItem() { throw new Error(); }, setItem() { throw new Error(); }}});
  blocked.setSystemEnabled(false);
  assert.equal(blocked.systemEnabled(), false, "存储不可用时仍保留本次选择");
  console.log("notifications: system preference, unread persistence, current-session suppression, dedup, desktop clicks and failure recovery passed");
}
main().catch(error => { console.error(error); process.exitCode = 1; });
