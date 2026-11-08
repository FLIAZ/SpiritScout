"use strict";
// Skill/MCP 目录共享筛选契约；保留 false、分页、时间与连接摘要脱敏。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../src/spiritscout/web/static/js/resources.js'), 'utf8');
const helpers = source.slice(source.indexOf('  function skillDefaults'), source.indexOf('  function shell'));
const context = vm.createContext(require('./helpers/i18n').context({URL, URLSearchParams, Date, esc: String}));
vm.runInContext(helpers, context);
for (const [defaults, filters] of [
  [context.skillDefaults, {domain:'web', source:'archive', added_from:'2026-10-01', added_to:'2026-10-28'}],
  [context.mcpDefaults, {transport:'http', source:'managed', risk:'readonly', agent:'ctf_web'}],
]) {
  const params=context.skillQuery({q:'接口',filters:{...defaults(),enabled:false,...filters}},40);
  for (const [key,value] of Object.entries({q:'接口',enabled:'false',offset:'40',...filters})) {
    assert.equal(params.get(key),value);
  }
}
const html = vm.runInContext('skillControlsHtml()', context);
assert.match(html, /data-skill-enabled="true" aria-pressed="true"/);
assert.match(html, /data-skill-enabled="false" aria-pressed="false"/);
assert.match(html, /data-skill-filter="date_state"/);
assert.match(html, /data-skill-reset/);
assert.match(vm.runInContext("skillMeta({domain:'web',source_kind:'archive',created_at:0})", context), /1970\/01\/01 08:00/);
const mcp=context.mcpControlsHtml();
for(const field of ['transport','source','risk','agent','date_state','added_from','added_to','sort'])assert(mcp.includes(`data-mcp-filter="${field}"`));
assert(!mcp.includes('data-skill-'));assert(!mcp.includes('value="builtin"'));assert(mcp.includes('data-mcp-enabled="false"'));assert(mcp.includes('data-mcp-reset'));
assert.equal(context.mcpConnection({transport:'http',url:'https://user:password@host.test/mcp?token=hidden#private'}),'https://host.test/mcp');
assert.equal(context.mcpConnection({transport:'stdio',command:'python'}),'python');
console.log('resource catalogs: filters, status groups, paging, timestamps and redacted connections passed');
