"use strict";
/* 模式仅决定常用入口；页面 ID 与旧链接保持兼容，不改变模型能力或执行流程。 */
const SessionNavigation = (() => {
  const primary = document.getElementById('s-tabs');
  const secondary = document.getElementById('session-secondary');
  const directory = document.getElementById('session-functions');
  const toggle = document.getElementById('session-functions-toggle');
  const families = {files: 'delivery', deliverables: 'delivery', report: 'delivery',
    events: 'records', traffic: 'records', background: 'records'};
  const presets = {src: ['assets', 'vulns'], ctf: ['flags'],
    competition: ['challenges', 'flags'], awdp: ['vulns']};
  const common = ['chat', 'topo', 'delivery', 'records'];
  const primaryButtons = [...primary.querySelectorAll('[data-tab]')];
  const secondaryButtons = [...secondary.querySelectorAll('[data-tab], [data-workspace-home]')];
  const buttons = [...primaryButtons, ...secondaryButtons];
  const destinations = [...document.querySelectorAll('[data-session-page]')];
  const descriptions = new Map([...directory.querySelectorAll('[data-session-page]')]
    .map(button => [button.dataset.sessionPage, button.querySelector('[data-function-description]').textContent]));
  let session = null, mode, sideSession = false, selected = 'chat';
  let featured = new Set(common);
  const family = tab => families[tab] || tab;
  function available(tab) {
    if (tab === 'files') return mode === 'src' && !sideSession;
    if (tab === 'report') return !!mode && mode !== 'src';
    if (tab === 'challenges') return mode === 'competition';
    return buttons.some(button => button.dataset.tab === tab);
  }
  function close() {
    if (directory.matches(':popover-open')) directory.hidePopover();
  }
  function sync(tab) {
    selected = tab;
    const group = family(tab);
    for (const button of primaryButtons) {
      const key = button.dataset.tab;
      button.hidden = !available(key) || (!featured.has(key) && key !== tab);
      button.dataset.contextual = String(!featured.has(key));
      const active = key === group;
      button.classList.toggle('on', active);
      button.setAttribute('aria-selected', String(active));
      button.setAttribute('aria-controls', 'tab-' + (active ? tab : key));
      button.tabIndex = active ? 0 : -1;
    }
    secondary.hidden = !['delivery', 'records'].includes(group);
    for (const area of secondary.querySelectorAll('[data-session-family]')) {
      area.hidden = area.dataset.sessionFamily !== group;
    }
    for (const button of secondaryButtons) {
      const key = button.dataset.tab || button.dataset.workspaceHome;
      button.hidden = !available(key);
      const active = key === tab;
      button.classList.toggle('on', active);
      button.setAttribute('aria-selected', String(active));
      button.tabIndex = active ? 0 : -1;
    }
    for (const button of directory.querySelectorAll('[data-session-page]')) {
      button.setAttribute('aria-current', button.dataset.sessionPage === tab ? 'page' : 'false');
    }
  }
  function configure(value) {
    const nextMode = value?.mode || null, nextSide = !!value?.side_session;
    if (mode === nextMode && sideSession === nextSide) return;
    mode = nextMode; sideSession = nextSide;
    featured = new Set([...common, ...(presets[mode] || [])]);
    for (const button of destinations) {
      const key = button.dataset.sessionPage;
      const enabled = available(key);
      const location = button.querySelector('[data-function-location]');
      const description = button.querySelector('[data-function-description]');
      button.disabled = !enabled;
      if (!button.closest('#session-functions')) button.hidden = !enabled;
      if (location) location.textContent = I18n.t(featured.has(key) ? '常用入口'
        : families[key] === 'delivery' ? '文件与报告'
          : families[key] === 'records' ? '运行记录' : '更多功能');
      if (description) description.textContent = enabled ? descriptions.get(key) : I18n.t(
        key === 'challenges' ? '平台题目列表由智能体竞赛模式提供。'
          : key === 'report' ? 'SRC 报告请从「报告文件」查看。'
            : '子 Agent 报告文件由非旁路 SRC 会话提供。');
    }
    sync(selected);
  }
  function reset(id, value) {
    close(); session = id; selected = 'chat';
    configure(value); sync('chat');
    update(null);
  }
  function write(node, value) {
    if (node.textContent !== value) node.textContent = value;
  }
  function update(state) {
    if (state?.session && state.session.session_id !== session) return;
    if (state?.session) configure(state.session);
    const counts = {vulns: state?.vulns?.total, flags: state?.flags?.total};
    const hints = [];
    for (const [key, value] of Object.entries(counts)) {
      const count = Number.isFinite(value) && value > 0 ? String(value) : '';
      for (const node of primary.querySelectorAll(`[data-session-count="${key}"]`)) {
        node.hidden = !count; write(node, count);
      }
      const entry = directory.querySelector(`[data-session-page="${key}"] [data-function-count]`);
      entry.hidden = !count; write(entry, count);
      if (count && !featured.has(key)) hints.push(I18n.t(key === 'vulns' ? '漏洞' : 'Flag') + ' ' + count);
    }
    document.getElementById('session-functions-dot').hidden = !hints.length;
    toggle.title = hints.length ? I18n.t('其他结果') + ' · ' + hints.join(' · ') : I18n.t('全部功能');
    toggle.setAttribute('aria-label', I18n.t('全部功能') + (hints.length ? ' · ' + hints.join(' · ') : ''));
    const status = state?.session;
    const tasks = state?.tasks;
    const values = {status: status ? (status.paused ? I18n.t('已暂停') : STATUS_CN[status.status] || status.status) : '—',
      tasks: tasks ? String(tasks.length) : '—',
      running: tasks ? String(tasks.filter(task => ['running', 'dispatched'].includes(task.status)).length) : '—'};
    for (const node of document.querySelectorAll('[data-session-summary]')) write(node, values[node.dataset.sessionSummary]);
  }
  for (const button of destinations) button.addEventListener('click', () => {
    if (!available(button.dataset.sessionPage)) return;
    close(); selectSessionTab(button.dataset.sessionPage);
    // 目录关闭后焦点落在当前页面的标签，不遗留在顶层弹层中。
    const target = buttons.find(tab => tab.dataset.tab === button.dataset.sessionPage && !tab.hidden);
    target?.focus({preventScroll: true});
  });
  for (const button of secondaryButtons) {
    button.id = 'session-tab-' + (button.dataset.tab || button.dataset.workspaceHome + '-overview');
    button.setAttribute('role', 'tab');
    button.setAttribute('aria-controls', 'tab-' + (button.dataset.tab || button.dataset.workspaceHome));
    if (button.dataset.workspaceHome) button.addEventListener('click', () => selectSessionTab(button.dataset.workspaceHome));
  }
  function arrowNavigation(event) {
    const list = event.currentTarget;
    const visible = [...list.querySelectorAll('[role="tab"]')].filter(button => !button.hidden && !button.closest('[hidden]'));
    const index = visible.indexOf(document.activeElement);
    if (index < 0) return;
    const keys = {ArrowRight: (index + 1) % visible.length,
      ArrowLeft: (index - 1 + visible.length) % visible.length, Home: 0, End: visible.length - 1};
    if (!Object.hasOwn(keys, event.key)) return;
    event.preventDefault(); visible[keys[event.key]].focus(); visible[keys[event.key]].click();
  }
  primary.addEventListener('keydown', arrowNavigation);
  for (const area of secondary.querySelectorAll('[role="tablist"]')) area.addEventListener('keydown', arrowNavigation);
  function positionDirectory() {
    if (!directory.matches(':popover-open')) return;
    const box = toggle.getBoundingClientRect();
    const left = Math.min(box.right - directory.offsetWidth, window.innerWidth - directory.offsetWidth - 16);
    directory.style.left = Math.max(16, left) + 'px';
    directory.style.top = Math.max(16, Math.min(box.bottom + 8, window.innerHeight - directory.offsetHeight - 16)) + 'px';
  }
  directory.addEventListener('toggle', event => {
    toggle.setAttribute('aria-expanded', String(event.newState === 'open'));
    positionDirectory();
  });
  window.addEventListener('resize', positionDirectory);
  return {reset, configure, sync, update, available, close};
})();
