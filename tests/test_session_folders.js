"use strict";
// 会话侧栏文件夹：按项目分组、未知项目归未分类、目录顺序与未分类殿后。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "../src/spiritscout/web/static/js/core.js"), "utf8");
const grouping = source.slice(
  source.indexOf("function sessionGroups"),
  source.indexOf("/* 折叠状态"));
assert.ok(grouping.includes("function sessionGroups"), "找不到 sessionGroups 分组函数");

const context = vm.createContext({});
vm.runInContext(grouping, context);
const group = (sessions, projects) => {
  context.__sessions = sessions;
  context.__projects = projects;
  return JSON.parse(vm.runInContext(
    "JSON.stringify(sessionGroups(__sessions, __projects))", context));
};

const projects = [
  {project_id: "prj-a", name: "BiliBili <SRC>"},
  {project_id: "prj-b", name: "直播"},
];
const sessions = [
  {session_id: "sess-1", project_id: "prj-a"},
  {session_id: "sess-2", project_id: ""},
  {session_id: "sess-3", project_id: "prj-gone"},
  {session_id: "sess-4", project_id: "prj-b"},
  {session_id: "sess-5", project_id: "prj-a"},
];

const groups = group(sessions, projects);
assert.deepEqual(groups.map(g => g.id), ["prj-a", "prj-b", ""],
  "项目按目录顺序，未分类殿后");
assert.deepEqual(groups.map(g => g.name), ["BiliBili <SRC>", "直播", "未分类"]);
assert.deepEqual(groups[0].items.map(s => s.session_id), ["sess-1", "sess-5"],
  "文件夹内保持会话原有时间序");
assert.deepEqual(groups[2].items.map(s => s.session_id), ["sess-2", "sess-3"],
  "无项目与指向已删项目的会话都归未分类");

const missingName = group([{session_id: "sess-9", project_id: "prj-a"}],
  [{project_id: "prj-a", name: ""}]);
assert.equal(missingName[0].name, "未命名项目", "空项目名有可读回退");

assert.deepEqual(group([], projects), [], "没有会话时不生成空文件夹");

console.log("session folders: passed");
