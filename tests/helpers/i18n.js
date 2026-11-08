const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// 既有回归保持默认中文，也执行真实的本地化实现。
const I18n = vm.runInNewContext(
  fs.readFileSync(path.join(__dirname, '../../src/spiritscout/web/static/js/i18n.js'), 'utf8') + '\nI18n',
  {URL, URLSearchParams}
);
exports.context = value => {
  value.I18n ??= I18n;
  value.URLSearchParams ??= URLSearchParams;
  return value;
};
