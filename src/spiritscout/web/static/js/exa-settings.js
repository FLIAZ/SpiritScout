"use strict";
/* 仅保留公开状态；密钥只存在于输入框和本次保存请求，不写入页面缓存。 */
const ExaSettings = (() => {
  let panel=null, settings=null, busy=false, version=0;
  const endpoint='/api/resources/search/exa';
  function mount(container) {
    if(!container || panel===container)return;
    panel=container;
    panel.className='exa-settings';
    panel.innerHTML=`<section class="exa-config" aria-labelledby="exa-title"><header><div><span class="resource-eyebrow">公开资料检索</span><h3 id="exa-title">Exa 搜索</h3></div><span class="resource-badge" data-exa-badge>正在读取…</span></header>
      <p class="exa-mode-note">仅供 SRC 和普通 CTF 模式使用。<strong>智能体模式禁止网页搜索。</strong></p>
      <form><label for="exa-api-key">Exa API Key</label><input id="exa-api-key" type="password" autocomplete="new-password" spellcheck="false" maxlength="4096" placeholder="粘贴 Exa API Key" aria-describedby="exa-key-help"><small id="exa-key-help">选填；密钥仅用于阻塞时的付费检索，加密保存；留空保留。新会话生效。</small>
      <div class="exa-actions"><button class="btn primary" type="submit" disabled>保存并启用</button><button class="btn" type="button" data-exa-disable disabled>停用</button><button class="btn" type="button" data-exa-clear hidden>清除已存密钥</button></div><p data-exa-status role="status"></p><p data-exa-error role="alert"></p></form></section>
      <aside class="exa-guide" aria-labelledby="exa-guide-title"><h3 id="exa-guide-title">如何获取 API Key</h3><ol><li>打开 <a href="https://dashboard.exa.ai/" target="_blank" rel="noopener noreferrer">Exa 官方控制台 ↗</a>，注册或登录。</li><li>进入 API Keys，创建并复制密钥。</li><li>粘贴到密钥输入框，点击“保存并启用”。</li></ol><p>默认复用缓存与免费检索；免费额度耗尽时可用公开搜索引擎。仅难题明显阻塞进度、模型说明原因后才调用付费 API。</p><a href="https://exa.ai/docs/get-started/exa-mcp" target="_blank" rel="noopener noreferrer">查看官方接入说明 ↗</a></aside>`;
    panel.querySelector('form').onsubmit=event=>{event.preventDefault();save('enable');};
    panel.querySelector('[data-exa-disable]').onclick=()=>save('disable');
    panel.querySelector('[data-exa-clear]').onclick=()=>save('clear');
  }
  function render() {
    if(!panel)return;
    const configured=settings?.key_configured;
    panel.querySelector('[data-exa-badge]').textContent=settings ? settings.enabled ? '免费优先 · SRC / CTF' : configured ? '已配置 · 已停用' : '搜索已停用' : '配置未载入';
    panel.querySelector('[type="submit"]').disabled=busy || !settings;
    panel.querySelector('[data-exa-disable]').disabled=busy || !settings?.enabled;
    panel.querySelector('[data-exa-clear]').hidden=!settings?.stored_key;
    panel.querySelector('[data-exa-clear]').disabled=busy;
    panel.querySelector('#exa-api-key').disabled=busy;
    panel.querySelector('#exa-api-key').placeholder=configured ? '已配置，留空保留；填写新密钥可替换' : '粘贴 Exa API Key';
    panel.querySelector('#exa-key-help').textContent=settings?.key_source==='environment' ? '当前使用服务器环境变量中的密钥；填写后改用加密保存的密钥。新会话生效。' : '选填；密钥仅用于阻塞时的付费检索，加密保存；留空保留。新会话生效。';
  }
  async function load() {
    if(!panel || busy)return;
    const requestVersion=++version;
    try {
      const data=await api(endpoint);if(requestVersion!==version)return;
      settings=data;panel.querySelector('[data-exa-error]').textContent='';render();
    } catch(error) {if(requestVersion===version){panel.querySelector('[data-exa-error]').textContent=`读取失败：${error.message}。请点击页面刷新重试。`;render();}}
  }
  async function save(action) {
    if(busy || !settings)return;
    const input=panel.querySelector('#exa-api-key');
    const body={enabled:action==='enable',expected_version:settings.version};
    if(action==='clear')body.clear_key=true;
    if(action==='enable' && input.value.trim())body.api_key_value=input.value.trim();
    input.value='';busy=true;version++;render();
    panel.querySelector('[data-exa-error]').textContent='';panel.querySelector('[data-exa-status]').textContent='正在保存…';
    try {
      settings=await api(endpoint,{method:'PUT',body:JSON.stringify(body)});
      panel.querySelector('[data-exa-status]').textContent=action==='enable' ? '已保存：SRC / CTF 免费优先，明显阻塞时才付费检索；智能体模式禁用。' : action==='clear' ? '已清除保存的密钥并停用搜索。' : '已停用搜索，新会话生效。';
    } catch(error) {
      panel.querySelector('[data-exa-status]').textContent='';
      panel.querySelector('[data-exa-error]').textContent=`保存失败：${error.message}。输入框已清空；请重新填写密钥后重试。`;
      busy=false;await load(); // 版本冲突时刷新公开状态，不携带或回显密钥。
      panel.querySelector('[data-exa-error]').textContent=`保存失败：${error.message}。输入框已清空；请重新填写密钥后重试。`;
    } finally {delete body.api_key_value;busy=false;render();}
  }
  function clear() {if(panel)panel.querySelector('#exa-api-key').value='';}
  return {mount,load,clear};
})();
