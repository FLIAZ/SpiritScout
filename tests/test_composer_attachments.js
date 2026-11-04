"use strict";
/* 回归：附件芯片绑定必须读「当前」S.compFiles/S.homeFiles。
 * 旧实现把数组引用捕获进 onchange 闭包，而切会话/发送后会整体换新数组，
 * 结果芯片照常显示、compSend 却读到空列表——附件静默丢失。 */
const assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
const source = fs.readFileSync("src/spiritscout/web/static/js/console.js", "utf8");
const nodes = new Map();
function node(id) {
  if (!nodes.has(id)) nodes.set(id, {value: "", textContent: "", innerHTML: "", dataset: {},
    disabled: false, files: [], classList: {toggle() {}}, setAttribute() {},
    querySelectorAll: () => []});
  return nodes.get(id);
}
const S = {cur: "sess-a", compFiles: [], homeFiles: []};
const ctx = vm.createContext({S, $: node, esc: s => String(s), fmtSize: () => "1 KB",
  syncComposerControl() {}});
const start = source.indexOf("/* 附件 chip");
// 同名监听器绑定在附件段之前，必须从 start 起找段落结尾
const end = source.indexOf("compInput.addEventListener(\"input\"", start);
assert.ok(start > 0 && end > start, "console.js 附件段落标记存在");
vm.runInContext(source.slice(start, end), ctx);

// 打开会话会整体换新数组（views.js openSession 的 S.compFiles = []）
S.compFiles = [];
node("#comp-file").files = [{name: "exp.zip", size: 100}];
node("#comp-file").onchange();
assert.equal(S.compFiles.length, 1, "onchange 必须写入当前 S.compFiles，而非绑定时的旧数组");
assert.match(node("#comp-files").innerHTML, /exp\.zip/, "选中的文件要出现在芯片区");

// 再次换新数组后仍可继续追加（多次切换会话的路径）
S.compFiles = [];
node("#comp-file").files = [{name: "dict.txt", size: 5}];
node("#comp-file").onchange();
assert.equal(S.compFiles[0].name, "dict.txt");

// 发起任务的附件走同一机制（console.js 发送后 S.homeFiles = []）
S.homeFiles = [];
node("#home-file").files = [{name: "题面.zip", size: 9}];
node("#home-file").onchange();
assert.equal(S.homeFiles.length, 1);
console.log("test_composer_attachments: 全部通过");
