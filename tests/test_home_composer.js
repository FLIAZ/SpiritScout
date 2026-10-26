const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(value = '') {
    this.value = value;
    this.checked = false;
    this.dataset = {};
    this.style = {};
    this.scrollHeight = 90;
    this.listeners = {};
    this.classes = new Set();
    this.classList = { toggle: (name, on) => on ? this.classes.add(name) : this.classes.delete(name) };
  }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  focus() { this.focused = true; }
  closest() { return null; }
}
const elements = Object.fromEntries(['c-go', 'c-mode', 'c-mode-hint', 'c-mission'].map(id => [`#${id}`, new Element()]));
const radios = ['src', 'ctf', 'competition'].map(mode => new Element(mode));
const examples = ['src', 'ctf', 'competition'].map(mode => {
  const element = new Element();
  element.dataset = { mode, mission: `示例 ${mode}` };
  element.textContent = '显示文字 ↗';
  return element;
});
const source = fs.readFileSync('src/spiritscout/web/static/js/console.js', 'utf8');
const home = source.slice(source.indexOf('const autogrow ='), source.indexOf('/* 附件暂存：'));
let launches = 0;
vm.runInNewContext(home, {
  $: selector => elements[selector],
  document: { querySelectorAll: selector => selector === '.chip-s' ? examples : radios },
  selxSync() {}, toast() {}, S: { homeFiles: [] },
  launchMission() { launches++; },
});
assert.equal(elements['#c-go'].classes.has('ready'), false);
for (const radio of radios) {
  radio.listeners.change();
  assert.equal(elements['#c-mode'].value, radio.value);
  assert.deepEqual(radios.filter(item => item.checked).map(item => item.value), [radio.value]);
}
elements['#c-mission'].value = '实际任务';
elements['#c-mission'].listeners.input();
assert.equal(elements['#c-go'].classes.has('ready'), true);
for (const example of examples) {
  example.onclick();
  assert.equal(elements['#c-mode'].value, example.dataset.mode);
  assert.equal(elements['#c-mission'].value, `${example.dataset.mission}：`);
  assert.equal(elements['#c-mission'].value.includes('↗'), false);
}
elements['#c-mission'].listeners.keydown({ key: 'Enter', isComposing: true });
assert.equal(launches, 0);
console.log('home composer: mode synchronization, examples, ready state and IME input passed');
