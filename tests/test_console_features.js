"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const root = path.resolve(__dirname, "../src/spiritscout/web/static/js");
const escape = value => String(value ?? "").replace(/[&<>\"]/g, char => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[char]));
function load(file, name) {
  const context = {console, esc: escape, modelDisplayName: value => String(value)};
  return vm.runInNewContext(fs.readFileSync(path.join(root, file), "utf8") + `\n${name}`, require('./helpers/i18n').context(context));
}
async function main() {
  const models = load("composer-model.js", "ComposerModels");
  const rows = models.options({models: [
    {key: "alpha-key", model: "same", provider_ref: "alpha"},
    {key: "beta-key", model: "same", provider_ref: "beta"},
    {key: "disabled", model: "old", enabled: false},
  ]});
  assert.equal(rows.length, 3);
  assert.equal(rows[1].key, "alpha-key"); assert.equal(rows[2].key, "beta-key");
  let sid = "one", chosen = "alpha-key", resolve;
  const saved = [];
  const ctl = models.create({request: () => new Promise(done => { resolve = done; }),
    sessionId: () => sid, sessionModel: () => chosen,
    saveSession: (id, model) => { saved.push([id, model]); if (id === sid) chosen = model; }});
  await ctl.choose("home", "beta-key"); assert.equal(ctl.selected("home"), "beta-key");
  const changing = ctl.choose("comp", "beta-key"); assert.equal(ctl.pending("comp"), true);
  sid = "two"; resolve({model: "beta-key"}); await changing;
  assert.equal(chosen, "alpha-key"); assert.equal(saved[0][0], "one");
  const rejected = models.create({request: async () => { throw new Error("fixture failure"); },
    sessionId: () => "one", sessionModel: () => chosen, saveSession: () => assert.fail("failure must not save")});
  await assert.rejects(rejected.choose("comp", "bad")); assert.equal(rejected.pending("comp"), false);

  const notify = load("notify.js", "ConsoleNotify");
  const values = new Map(), raised = [], notices = [];
  const storage = {getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value)};
  const doc = {title: "fixture", visibilityState: "hidden"};
  const items = Array.from({length: 5}, (_, index) => ({id: String(index), title: "fixture", message: "fixture", session_id: "one"}));
  const notifications = notify.create({fetchItems: async () => ({items}), storage,
    notifyCtor: null, doc, win: {focus() {}}, toastFn: text => raised.push(text),
    isCurrent: () => false, openSession: id => notices.push(id)});
  await notifications.check(); assert.equal(raised.length, 4);
  await notifications.check(); assert.equal(raised.length, 4);
  notifications.clearFlash(); assert.equal(doc.title, "fixture");
  notifications.setEnabled(false); notifications.clearSeen();
  await notifications.check(); assert.equal(raised.length, 4);
  const blocked = notify.create({fetchItems: async () => ({items}),
    storage: {getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); }},
    notifyCtor: null, doc, win: {}, toastFn() {}, isCurrent: () => true, openSession() {}});
  doc.visibilityState = "visible"; await blocked.check();
  assert.equal(doc.title, "fixture");
  let permissions = 0, sent = 0;
  class Notification { static permission = "granted"; static async requestPermission() { permissions++; } constructor() { sent++; } }
  const system = notify.create({fetchItems: async () => ({items: [items[0]]}), storage: null,
    notifyCtor: Notification, doc: {title: "system", visibilityState: "hidden"}, win: {}, toastFn() {}, isCurrent: () => false, openSession() {}});
  await system.check(); assert.equal(sent, 1); assert.equal(permissions, 0);
  await system.requestPermission(); assert.equal(permissions, 1);

  const experience = load("session-experience.js", "SessionExperience");
  assert.match(experience.historyRows([{result: "duplicate", duplicates: 2}]), /与已有经验重复/);
  assert.match(experience.historyRows([{result: "unparsed", error: "<failure>"}]), /&lt;failure&gt;/);
  assert.match(experience.cards([{insight: "fixture", sources: [{ref: "topology:one@1", source_session: "sess-one"}]}]), /topology:one@1/);
  console.log("console features: model choices, stale responses, notification fallback/dedup, experience rendering passed");
}
main().catch(error => { console.error(error); process.exitCode = 1; });
