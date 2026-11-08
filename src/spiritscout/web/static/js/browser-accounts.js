/* 账号凭据只写不回显；仅打开管理页时读取账号公开摘要。 */
const BrowserAccountsPage = (() => {
  let items=[], generation=0, editing=null;
  const methods={none:'无二次验证',sms:'短信验证码',email:'邮箱验证码',authenticator:'验证器动态码'};
  const root=()=>document.querySelector('#view-browser-accounts');
  const dialog=()=>document.querySelector('#browser-account-editor');
  const field=name=>dialog().querySelector(`[name="${name}"]`);
  const date=value=>value?new Date(value*1000).toLocaleString(I18n.dateLocale):I18n.t('尚未保存');
  function mount() {
    if(root().dataset.ready)return;
    root().dataset.ready='yes';
    root().innerHTML=I18n.html`<header class="resource-head"><div><h2>浏览器账号</h2><p class="muted">管理网站账号和登录态。凭据加密保存，每个账号使用独立浏览器。</p></div><div class="resource-head-actions"><button class="btn" data-refresh>刷新</button><button class="btn primary" data-add>添加账号</button></div></header><p role="status" data-status></p><div class="browser-account-list"></div>`;
    root().querySelector('[data-refresh]').onclick=load;
    root().querySelector('[data-add]').onclick=()=>edit(null);
    document.body.insertAdjacentHTML('beforeend',I18n.html`<dialog id="browser-account-editor" class="account-editor"><form>
      <header><h3>浏览器账号设置</h3><button type="button" class="btn sm" data-close aria-label="关闭">关闭</button></header>
      <div class="account-fields"><label>名称<input name="name" required maxlength="100" placeholder="例如：测试平台 · 普通用户"></label><label>身份说明<input name="role" maxlength="100" placeholder="例如：普通用户 A / 管理员"></label></div>
      <label>网站地址<textarea name="origins" required rows="2" placeholder="https://app.example.com&#10;多个登录网站每行一个"></textarea></label>
      <fieldset><legend>账号密码</legend><p class="muted">留空保留已保存凭据；密码加密保存，Agent 可直接填写到上述网站。</p><div class="account-fields"><label>用户名<input name="username" autocomplete="off" maxlength="4096"></label><label>密码<input type="password" name="password" autocomplete="new-password" maxlength="4096"></label></div><label class="account-check"><input type="checkbox" name="clear_credentials">清除已保存的账号密码</label></fieldset>
      <fieldset><legend>二次验证</legend><label>验证方式<select name="mfa_method"><option value="none">无二次验证</option><option value="sms">短信验证码</option><option value="email">邮箱验证码</option><option value="authenticator">验证器动态码</option></select></label><label>手机号或邮箱提示（选填）<input name="mfa_destination" autocomplete="off" maxlength="4096" placeholder="用于提醒接收位置，不会在任务中公开完整内容"></label><p class="muted" data-mfa-help></p></fieldset>
      <fieldset><legend>登录态缓存</legend><label class="account-check"><input type="checkbox" name="retain">保留 Cookie 和网站本地存储</label><label>缓存有效天数<input type="number" name="ttl_days" min="1" max="365" required value="30"></label><p class="muted">缓存期限不延长平台会话。失效后可使用已保存的凭据重新登录；验证码由你提供。</p><details><summary>导入登录态（可选）</summary><label>Playwright Storage State JSON<textarea name="storage_state" rows="4" spellcheck="false" placeholder='{"cookies":[],"origins":[]}'></textarea></label></details></fieldset>
      <p role="alert" data-error></p><footer><button type="submit" class="btn primary">保存账号</button></footer>
    </form></dialog>`);
    dialog().querySelector('[data-close]').onclick=()=>dialog().close();
    dialog().addEventListener('close',()=>{for(const name of ['username','password','mfa_destination','storage_state'])field(name).value='';editing=null;});
    field('mfa_method').onchange=()=>{
      const method=field('mfa_method').value;
      field('mfa_destination').disabled=method==='none'||method==='authenticator';
      dialog().querySelector('[data-mfa-help]').textContent=I18n.t(method==='authenticator'?'需要验证时，从你的手机验证器读取动态码并通过会话提交。':method==='none'?'登录时无需额外验证码。':'Agent 在网站请求验证码，你通过会话的验证输入框提交。');
    };
    dialog().querySelector('form').onsubmit=save;
  }
  function render() {
    root().querySelector('.browser-account-list').innerHTML=items.map((a,i)=>I18n.html`<article class="browser-account-card"><header><div><h3>${esc(a.name)}</h3><p>${esc(a.role||I18n.t('身份说明未设置'))}</p></div><span class="tag">${esc(I18n.t(methods[a.mfa_method]||methods.none))}</span></header><p class="account-sites">${a.origins.map(esc).join(' · ')}</p><dl><div><dt>账号凭据</dt><dd>${a.credentials_configured?esc(a.username_hint):I18n.t('未保存')}</dd></div><div><dt>登录态缓存</dt><dd>${I18n.t(({available:'已缓存，使用前需验证',expired:'已过期，需重新登录',empty:'尚无缓存'})[a.cache_status])}</dd></div><div><dt>缓存截止</dt><dd>${esc(date(a.expires_at))}</dd></div></dl><div class="account-actions"><button class="btn sm" data-edit="${i}">编辑</button><button class="btn sm" data-action="open" data-index="${i}">${a.opened?I18n.t('显示登录窗口'):I18n.t('打开登录窗口')}</button><button class="btn sm" data-action="capture" data-index="${i}" ${!a.opened||!a.retain?'disabled':''}>保存登录态</button><details><summary>更多</summary><button class="btn sm" data-action="close" data-index="${i}" ${!a.opened?'disabled':''}>关闭窗口</button><button class="btn sm" data-action="clear" data-index="${i}">清除缓存</button><button class="btn sm" data-action="delete" data-index="${i}">删除账号</button></details></div></article>`).join('')||I18n.html('<div class="resource-empty">添加第一个网站账号，配置登录凭据或手动登录后保存 Cookie。</div>');
    root().querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>edit(items[Number(b.dataset.edit)]));
    root().querySelectorAll('[data-action]').forEach(b=>b.onclick=()=>act(b));
  }
  async function load() {
    if(window.__SPIRITSCOUT_LIGHT__)return;
    mount();const ticket=++generation;
    root().querySelector('[data-status]').textContent=I18n.t('正在读取…');
    try{const data=await api('/api/browser-accounts');if(ticket!==generation)return;items=data.items;render();root().querySelector('[data-status]').textContent='';}
    catch(e){if(ticket===generation)root().querySelector('[data-status]').textContent=I18n.t('读取失败：')+e.message;}
  }
  function edit(account) {
    editing=account;dialog().querySelector('form').reset();
    for(const name of ['name','role'])field(name).value=account?.[name]||'';
    field('origins').value=(account?.origins||[]).join('\n');
    field('retain').checked=account?.retain??true;field('ttl_days').value=account?.ttl_days||30;
    field('mfa_method').value=account?.mfa_method||'none';field('mfa_method').onchange();
    if(typeof selxSync==='function')selxSync(field('mfa_method'));
    field('username').placeholder=account?.username_hint||I18n.t('未保存');
    field('password').placeholder=account?.credentials_configured?I18n.t('已保存，留空保持'):I18n.t('未保存');
    field('mfa_destination').placeholder=account?.mfa_destination_hint||I18n.t('手机号或邮箱提示');
    dialog().querySelector('[data-error]').textContent='';dialog().showModal();field('name').focus();
  }
  async function save(event) {
    event.preventDefault();const button=dialog().querySelector('[type="submit"]');button.disabled=true;
    try{
      const data={name:field('name').value.trim(),role:field('role').value.trim(),origins:field('origins').value.split(/[\n,，]+/).map(v=>v.trim()).filter(Boolean),retain:field('retain').checked,ttl_days:Number(field('ttl_days').value),mfa_method:field('mfa_method').value,clear_credentials:field('clear_credentials').checked};
      for(const name of ['username','password','mfa_destination'])if(field(name).value)data[name]=field(name).value;
      if(field('storage_state').value.trim())data.storage_state=JSON.parse(field('storage_state').value);
      const id=editing?.id||'account-'+crypto.randomUUID();
      await api('/api/browser-accounts/'+encodeURIComponent(id),{method:'PUT',body:JSON.stringify({data,expected_version:editing?.version||0})});
      dialog().close();toast(I18n.t('账号已保存'));await load();
    }catch(e){dialog().querySelector('[data-error]').textContent=e instanceof SyntaxError?I18n.t('登录态 JSON 格式无效'):e.message;}
    finally{button.disabled=false;}
  }
  async function act(button) {
    const account=items[Number(button.dataset.index)],action=button.dataset.action;
    if(['clear','delete'].includes(action)&&!confirm(action==='delete'?I18n.t('删除账号及已保存的登录态和凭据？'):I18n.t('清除缓存并关闭管理窗口？运行中会话的旧缓存将不能重新覆盖本次清除。')))return;
    button.disabled=true;
    try{await api(`/api/browser-accounts/${encodeURIComponent(account.id)}/${action}`,{method:'POST',body:JSON.stringify({expected_version:account.version})});toast(I18n.t(action==='capture'?'登录态已保存，仍需核对实际身份':action==='open'?'请在浏览器完成登录，再点击保存登录态':'操作已完成'));await load();}
    catch(e){toast(e.message,true);button.disabled=false;}
  }
  return {load,navigate:view=>{if(view==='browser-accounts')return load();}};
})();
