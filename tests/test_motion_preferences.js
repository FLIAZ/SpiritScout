"use strict";
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../src/spiritscout/web/static/js/motion.js"), "utf8");

function fixture(stored = null, {readFails = false, writeFails = false, reduced = false} = {}) {
  const nodes = new Map(), writes = [], animations = [], windowEvents = {}, documentEvents = {};
  const make = () => ({dataset: {}, textContent: "", hidden: false, handlers: {},
    setAttribute(key, value) { this[key] = value; },
    toggleAttribute(key, value) { this[key] = value; },
    addEventListener(key, fn) { this.handlers[key] = fn; }, focus() { this.focused = true; }});
  const node = id => { if (!nodes.has(id)) nodes.set(id, make()); return nodes.get(id); };
  const root = make(), dialog = node("motion-dlg");
  const inputs = ["auto", "soft", "quick", "off"].map(value => ({...make(), value, checked: false}));
  dialog.querySelectorAll = () => inputs;
  dialog.showModal = () => { dialog.open = true; };
  dialog.close = () => { dialog.open = false; dialog.handlers.close(); };
  node("motion-sample").animate = (_frames, options) => {
    const animation = {options, cancelled: false, cancel() { this.cancelled = true; }};
    animations.push(animation); return animation;
  };
  const storage = {
    getItem() { if (readFails) throw Error("blocked"); return stored; },
    setItem(key, value) { if (writeFails) throw Error("full"); writes.push([key, value]); stored = value; },
  };
  const media = {matches: reduced, addEventListener(_name, fn) { this.changed = fn; }};
  const doc = {documentElement: root, hidden: false, getElementById: node,
    addEventListener: (name, fn) => { documentEvents[name] = fn; }};
  const context = {document: doc, window: {localStorage: storage, matchMedia: () => media,
    addEventListener: (name, fn) => { windowEvents[name] = fn; }},
    getComputedStyle: () => ({getPropertyValue: key => ({"--motion-distance": "6px", "--motion-enter": "240ms", "--motion-ease": "ease-out"}[key])})};
  vm.runInNewContext(source, require('./helpers/i18n').context(context));
  return {root, node, inputs, dialog, media, animations, doc, writes,
    open: () => node("btn-motion").onclick(),
    choose(value) { const input = inputs.find(input => input.value === value); input.checked = true; input.handlers.change(); },
    visibility(hidden) { doc.hidden = hidden; documentEvents.visibilitychange(); },
    otherTab(value, key = "ss-motion") { windowEvents.storage({storageArea: storage, key, newValue: value}); }};
}

for (const stored of [null, "auto", "soft", "quick", "off", "garbage", "__proto__"]) {
  const f = fixture(stored), expected = ["soft", "quick", "off"].includes(stored) ? stored : "auto";
  assert.equal(f.root.dataset.motion, expected);
  assert.equal(f.inputs.filter(input => input.checked).length, 1);
  assert.equal(f.writes.length, 0, "启动不覆盖已保存的偏好");
}
const f = fixture("soft");
f.open();
assert.equal(f.dialog.open, true);
assert.equal(f.inputs.find(input => input.checked).focused, true);
assert.equal(f.node("btn-motion")["aria-expanded"], "true");
assert.equal(f.animations.length, 1);
f.choose("quick");
assert.equal(f.root.dataset.motion, "quick");
assert.deepEqual(f.writes, [["ss-motion", "quick"]]);
assert.equal(f.animations[0].cancelled, true, "切换节奏先取消上一段预览");
f.choose("off");
assert.equal(f.animations.length, 2, "关闭后不创建预览动画");
assert.equal(f.animations[1].cancelled, true);
assert.equal(f.node("motion-sample").textContent, "即时呈现");
f.choose("soft");
f.media.matches = true; f.media.changed();
assert.equal(f.root.dataset.motion, "soft", "系统减弱优先但不改写用户保存的选择");
assert.equal(f.node("motion-system-note").hidden, false);
assert.equal(f.animations.at(-1).cancelled, true);
const before = f.animations.length;
f.node("motion-replay").onclick(); assert.equal(f.animations.length, before);
f.media.matches = false; f.media.changed();
assert.equal(f.node("motion-system-note").hidden, true);
f.visibility(true);
assert.equal(f.root["data-page-hidden"], true);
assert.equal(f.animations.at(-1).cancelled, true);
f.visibility(false); assert.equal(f.root["data-page-hidden"], false);
f.dialog.close(); assert.equal(f.node("btn-motion")["aria-expanded"], "false");
const writes = f.writes.length;
f.otherTab("off"); assert.equal(f.root.dataset.motion, "off");
f.otherTab("soft", "ss-theme"); assert.equal(f.root.dataset.motion, "off");
f.otherTab(null, null); assert.equal(f.root.dataset.motion, "auto", "清空浏览器偏好后恢复默认");
assert.equal(f.writes.length, writes, "跨标签页同步不产生写回循环");
const unavailable = fixture("soft", {readFails: true, writeFails: true});
assert.equal(unavailable.root.dataset.motion, "auto");
unavailable.choose("quick");
assert.equal(unavailable.root.dataset.motion, "quick", "存储不可用也立即应用选择");
assert.match(unavailable.node("motion-save-note").textContent, /暂时无法保存/);
console.log("motion preferences: persistence, invalid settings, preview cancellation, reduced motion, visibility and cross-tab sync passed");
