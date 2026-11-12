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
const elements = Object.fromEntries(['c-go', 'c-mode', 'c-mode-hint', 'c-mission', 'c-multiple-answers-option', 'c-multiple-answers', 'c-ctf-types', 'c-ctf-type-note', 'c-ctf-challenge', 'c-ctf-name', 'c-ctf-description', 'c-ctf-hint', 'c-ctf-address'].map(id => [`#${id}`, new Element()]));
const radios = ['src', 'ctf', 'competition'].map(mode => new Element(mode));
const examples = ['src', 'ctf', 'competition'].map(mode => {
  const element = new Element();
  element.dataset = { mode, mission: `示例 ${mode}` };
  element.textContent = '显示文字 ↗';
  return element;
});
const source = fs.readFileSync('src/spiritscout/web/static/js/console.js', 'utf8');
const home = source.slice(source.indexOf('const autogrow ='), source.indexOf('/* 附件暂存：'));
let launches = 0, lastLaunch;
const context=vm.createContext(require('./helpers/i18n').context({
  $: selector => elements[selector],
  document: { querySelectorAll: selector => selector === '.chip-s' ? examples : selector.includes('home-mode') ? radios : [] },
  selxSync() {}, toast() {}, S: { homeFiles: [] },
  launchMission(payload, files, draft) { launches++; lastLaunch={payload, files, draft}; },
}));
vm.runInContext(home, context);
assert.equal(elements['#c-go'].classes.has('ready'), false);
for (const radio of radios) {
  radio.listeners.change();
  assert.equal(elements['#c-mode'].value, radio.value);
  assert.equal(elements['#c-multiple-answers-option'].hidden, radio.value !== 'ctf');
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

(async()=>{
  elements['#c-mode'].value='ctf';elements['#c-mode'].onchange();
  elements['#c-mission'].value='';
  elements['#c-ctf-name'].value='  题目 <script>原文</script>  ';
  elements['#c-ctf-name'].listeners.input();
  assert.equal(elements['#c-go'].classes.has('ready'),true,'challenge data alone can start a CTF');
  assert.equal(elements['#c-ctf-challenge'].hidden,false);
  elements['#c-ctf-description'].value='第一行题面\n第二行题面';
  elements['#c-ctf-hint'].value='赛方提示';
  elements['#c-ctf-address'].value='nc fixture.test 31337';
  elements['#c-multiple-answers'].checked=true;
  await elements['#c-go'].onclick();
  assert.equal(lastLaunch.payload.title,'题目 <script>原文</script>');
  for(const value of ['题目名称：','第一行题面\n第二行题面','题目提示：\n赛方提示','题目地址：\nnc fixture.test 31337'])
    assert.ok(lastLaunch.payload.mission.includes(value));
  assert.equal(lastLaunch.payload.multiple_answers,true);
  elements['#c-mission'].value='保留补充要求';
  await elements['#c-go'].onclick();
  assert.ok(lastLaunch.payload.mission.endsWith('保留补充要求'));
  elements['#c-mode'].value='src';elements['#c-mode'].onchange();
  await elements['#c-go'].onclick();
  assert.equal(lastLaunch.payload.mission,'保留补充要求','other modes never include CTF drafts');
  assert.equal(lastLaunch.payload.multiple_answers,false);
  assert.equal(lastLaunch.draft,null);
  assert.equal(elements['#c-ctf-name'].value,'  题目 <script>原文</script>  ','mode changes preserve the draft');
  elements['#c-mode'].value='ctf';elements['#c-mode'].onchange();
  const draft=vm.runInContext('ctfChallengeDraft()',context);
  context.api=async()=>{throw new Error('create failed');};
  context.openSession=()=>{};context.refreshSessions=()=>{};
  const launchSource=source.slice(source.indexOf('async function launchMission('),source.indexOf('/* 规则冲突即时确认：'));
  vm.runInContext(launchSource,context);
  await context.launchMission({mode:'ctf',mission:'fixture'},[],draft);
  assert.equal(elements['#c-ctf-hint'].value,'赛方提示','failed creation preserves all challenge inputs');
  let resolveCreate;
  context.api=()=>new Promise(resolve=>{resolveCreate=resolve;});
  const pending=context.launchMission({mode:'ctf',mission:'fixture'},[],draft);
  elements['#c-ctf-hint'].value='提交期间的新提示';
  resolveCreate({session_id:'fixture'});await pending;
  assert.equal(elements['#c-ctf-name'].value,'','successful creation clears submitted fields');
  assert.equal(elements['#c-ctf-hint'].value,'提交期间的新提示','new edits survive asynchronous creation');
  console.log('CTF challenge card: optional fields, complete mission, multi-answer setting, mode isolation and draft lifecycle passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
