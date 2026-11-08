const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '../src/spiritscout/web/static/js');
const dictionary = fs.readFileSync(path.join(root, 'i18n.en.js'), 'utf8');
const source = fs.readFileSync(path.join(root, 'i18n.js'), 'utf8');

function load({saved, search = '', blocked = false} = {}) {
  const writes = [];
  const context = vm.createContext({URL, URLSearchParams,
    location: {search, href: `http://localhost:8017/${search}`},
    localStorage: {
      getItem() { if (blocked) throw Error('storage denied'); return saved; },
      setItem(...args) { if (blocked) throw Error('storage denied'); writes.push(args); }
    }
  });
  vm.runInContext(dictionary + '\n' + source, context);
  return {api: vm.runInContext('I18n', context), context, writes};
}

const zh = load();
assert.equal(zh.api.locale, 'zh-CN');
assert.equal(zh.api.t`共有 ${3} 个会话`, '共有 3 个会话');
const en = load({saved: 'en'});
assert.equal(en.api.locale, 'en');
assert.equal(en.api.dateLocale, 'en-US');
assert.equal(en.api.t('未分类'), 'Unassigned');
assert.equal(en.api.t('  全部项目\n'), '  All projects\n');
assert.equal(en.api.t('未收录的原文'), '未收录的原文');
assert.equal(load({saved: 'en', search: '?lang=zh-CN'}).api.locale, 'zh-CN');
assert.equal(load({saved: 'en', search: '?lang=invalid'}).api.locale, 'en');
assert.equal(load({saved: 'invalid'}).api.locale, 'zh-CN');
const denied = load({blocked: true, search: '?lang=en'});
assert.equal(denied.api.locale, 'en');
assert.equal(denied.api.url('/login?next=console#form'), '/login?next=console&lang=en#form');
assert.deepEqual(load({search: '?lang=en'}).writes, [['ss-language', 'en']]);

// 译文可重排插值；重复渲染复用模板时也不能串用前一次的数据。
// 用词表中的真实模板，避免把业务数据当成界面文案继续翻译。
const modelNotice = (name, count) => en.api.t`来自 ${name}，共 ${count} 个可用模型。
        勾选后批量启用，之后可在模型行里逐个调整画像。`;
assert.equal(modelNotice('未分类', 3), "3 models available from 未分类. Select models to enable in bulk; you can adjust each model's capabilities afterward.");
assert.equal(modelNotice('全部项目', 7), "7 models available from 全部项目. Select models to enable in bulk; you can adjust each model's capabilities afterward.");
const markup = (name, content) => en.api.html`<button title="刷新">${name}</button><p>未分类</p>${content}`;
assert.equal(markup('未分类', '<pre>全部项目 &lt;script&gt;</pre>'),
  '<button title="Refresh">未分类</button><p>Unassigned</p><pre>全部项目 &lt;script&gt;</pre>');
assert.equal(markup('全部项目', '&lt;img src=x onerror=alert(1)&gt;'),
  '<button title="Refresh">全部项目</button><p>Unassigned</p>&lt;img src=x onerror=alert(1)&gt;');
assert.equal(en.api.html('<input value="未分类" placeholder="未分类" data-name="全部项目">'),
  '<input value="未分类" placeholder="Unassigned" data-name="全部项目">');
assert.equal(en.api.html`<p>${en.api.t('未分类')}</p>`, '<p>Unassigned</p>');
assert.equal(zh.api.html`<p title="刷新">${'全部项目'}</p>`, '<p title="刷新">全部项目</p>');

const catalog = vm.runInContext('EN_MESSAGES', en.context);
assert.ok(Object.keys(catalog).length > 2000);
for (const [key, value] of Object.entries(catalog)) {
  assert.ok(typeof value === 'string' && value.trim(), `Empty translation: ${key}`);
  const slots = text => (text.match(/\{\d+\}/g) || []).sort();
  assert.deepEqual(slots(value), slots(key), `Placeholder mismatch: ${key}`);
  assert.ok(!/[\u3400-\u9fff]/.test(value), `Untranslated copy: ${key}`);
}
console.log('i18n: language preferences, protected interpolation, HTML boundaries, and catalog passed');
