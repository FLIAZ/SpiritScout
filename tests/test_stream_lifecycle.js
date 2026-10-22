"use strict";
const assert = require('node:assert/strict');
const fs = require('node:fs'), vm = require('node:vm'), path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../src/spiritscout/web/static/js/views.js'), 'utf8');
let refreshes = 0, closed = 0, cleared = 0;
class FakeSource {
  constructor() { this.handlers = {}; }
  addEventListener(type, handler) { this.handlers[type] = handler; }
  close() { closed++; }
}
const sandbox = vm.createContext({S:{cur:'one',evSeen:0},EventSource:FakeSource,
  SessionHistory:{clearLiveQueue(){},async drain(){}},
  Date, refreshState(){refreshes++;},setInterval(){return 42;},clearInterval(){cleared++;}});
vm.runInContext(source.slice(source.indexOf('function startStreams('), source.indexOf('// 唤醒即自救')), sandbox);
(async () => {
vm.runInContext('startStreams(false)', sandbox);
const old = sandbox.S.es;
await old.handlers.done();
assert.equal(closed,1);assert.equal(cleared,1);assert.equal(refreshes,1);assert.equal(sandbox.S.es,null);
vm.runInContext('startStreams(false)', sandbox);
const next = sandbox.S.es;
await old.handlers.done();
assert.equal(sandbox.S.es,next,'旧连接结束事件不能关闭新连接');assert.equal(refreshes,1);
await next.handlers.done();assert.equal(closed,2);assert.equal(refreshes,2);
console.log('streams: terminal closes reconnect loop, resumed connection and stale event isolation passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
