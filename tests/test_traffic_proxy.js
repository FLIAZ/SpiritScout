"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const headerSource=fs.readFileSync(require('node:path').join(__dirname,'../src/spiritscout/web/static/js/views.js'),'utf8');
const header={};
const headerContext=vm.createContext(require('./helpers/i18n').context({$:()=>header}));
const renderRoute=vm.runInContext(`(${headerSource.match(/function renderSessionProxy\(session\) \{[\s\S]*?\n\}/)[0]})`,headerContext);
renderRoute({mode:'src',proxy_route:{id:'proxy-fixed',name:'创建时的方案',exit_ip:'8.8.8.8',
  nodes:[{name:'节点 A',server:'proxy.test',port:8080}]}});
assert.equal(header.hidden,false);
assert.match(header.textContent,/创建时的方案/);
assert.match(header.title,/proxy-fixed/);assert.match(header.title,/proxy.test:8080/);assert.match(header.title,/8\.8\.8\.8/);
renderRoute({mode:'src',proxy_route:{id:'direct'}});assert.match(header.textContent,/直连/);
renderRoute({mode:'src'});assert.match(header.textContent,/未记录/,'缺少历史快照不能冒充直连');
renderRoute({mode:'ctf',proxy_route:{id:'direct'}});assert.equal(header.hidden,true);
console.log('SRC route header: saved plan, chain, startup exit IP, direct and missing history passed');
