"use strict";
/* 技能文件浏览与 Markdown 编辑；文件只保存到资源库，不执行包内脚本。 */
const SkillBrowser = (() => {
  let dialog,current=null,selected='',raw=false,editing=false,saving=false,draft='',editor=null,onSaved=null,editConfig=null,legacy=false;
  function fileTree(files) {
    const root={children:new Map()};
    for(const path of Object.keys(files)){
      let node=root;const parts=path.split('/');
      parts.forEach((name,index)=>{
        if(!node.children.has(name))node.children.set(name,{name,path:parts.slice(0,index+1).join('/'),children:new Map(),file:index===parts.length-1});
        node=node.children.get(name);
      });
    }
    const render=node=>Array.from(node.children.values()).sort((a,b)=>Number(a.file)-Number(b.file)||a.name.localeCompare(b.name)).map(child=>child.file
      ?`<li><button type="button" data-skill-file="${esc(child.path)}" title="${esc(child.path)}"><span aria-hidden="true">▤</span>${esc(child.name)}</button></li>`
      :`<li><details open><summary><span aria-hidden="true">▱</span>${esc(child.name)}</summary><ul>${render(child)}</ul></details></li>`).join('');
    return `<ul>${render(root)}</ul>`;
  }
  function resolveFile(link,from,files) {
    try{
      const url=new URL(link,'https://skill.invalid/'+from);
      if(url.origin!=='https://skill.invalid')return null;
      const path=decodeURIComponent(url.pathname.slice(1));
      return Object.prototype.hasOwnProperty.call(files,path)?path:null;
    }catch{return null;}
  }
  function mount() {
    if(dialog)return;
    dialog=document.createElement('dialog');dialog.id='skill-browser';dialog.setAttribute('aria-labelledby','skill-browser-title');
    dialog.innerHTML=`<header class="skill-browser-header"><div><p>Skill 文件浏览器</p><h2 id="skill-browser-title"></h2><small data-skill-file-summary></small></div><div><button class="btn" type="button" data-skill-edit>编辑配置</button><button class="btn" type="button" data-skill-close aria-label="关闭技能文件浏览器">关闭</button></div></header>
      <div class="skill-browser-workspace"><nav class="skill-browser-tree" aria-label="技能目录"><details open><summary data-skill-root></summary><div data-skill-tree></div></details></nav>
      <section class="skill-browser-document" aria-label="文件内容"><header><p data-skill-current-path></p><div class="skill-file-actions"><button type="button" class="btn sm" data-skill-source aria-pressed="false">查看源文件</button><button type="button" class="btn sm" data-skill-write>编辑正文</button><button type="button" class="btn sm" data-skill-cancel hidden>取消编辑</button><button type="button" class="btn primary sm" data-skill-save hidden>保存修改</button></div></header>
      <div class="skill-editor-bar" hidden><div class="skill-editor-format" role="group" aria-label="正文格式"><button type="button" class="btn sm" data-skill-format="bold" aria-label="加粗所选文字"><b>粗体</b></button><button type="button" class="btn sm" data-skill-format="italic" aria-label="斜体所选文字"><i>斜体</i></button><button type="button" class="btn sm" data-skill-add-paragraph>新增段落</button><small>直接点击正文修改；粘贴使用纯文本。</small></div><p data-skill-edit-status role="status"></p></div>
      <div class="skill-browser-content" tabindex="0"></div></section></div>`;
    document.body.append(dialog);
    dialog.querySelector('[data-skill-close]').onclick=requestClose;
    dialog.addEventListener('cancel',event=>{event.preventDefault();requestClose();});
    dialog.querySelector('[data-skill-source]').onclick=()=>{if(saving)return;if(editing)draft=readDraft();raw=!raw;renderFile();};
    dialog.querySelector('[data-skill-write]').onclick=()=>{editing=true;draft=current.files[selected];renderFile();editor?.focus();dialog.querySelector('.skill-source-editor')?.focus();};
    dialog.querySelector('[data-skill-cancel]').onclick=()=>{editing=false;draft='';renderFile();setStatus('已取消修改');};
    dialog.querySelector('[data-skill-save]').onclick=saveFile;
    dialog.querySelector('[data-skill-add-paragraph]').onclick=()=>editor?.addParagraph();
    dialog.querySelectorAll('[data-skill-format]').forEach(button=>{button.onmousedown=event=>event.preventDefault();button.onclick=()=>editor?.command(button.dataset.skillFormat);});
    dialog.querySelector('[data-skill-edit]').onclick=()=>{if(!allowDiscard())return;const data=current,callback=editConfig;dialog.close();callback?.(data);};
    dialog.addEventListener('close',()=>{destroyEditor();current=null;editing=false;draft='';onSaved=null;editConfig=null;dialog.querySelector('.skill-browser-content').replaceChildren();dialog.querySelector('[data-skill-tree]').replaceChildren();});
    dialog.addEventListener('click',event=>{
      const file=event.target.closest('[data-skill-file]');
      if(file){event.preventDefault();if(!current||!Object.prototype.hasOwnProperty.call(current.files,file.dataset.skillFile)||file.dataset.skillFile===selected||!allowDiscard())return;editing=false;draft='';selected=file.dataset.skillFile;raw=false;renderFile();}
      else if(editing&&event.target.closest('.skill-browser-content a'))event.preventDefault();
    });
    window.addEventListener('beforeunload',event=>{if(hasChanges()){event.preventDefault();event.returnValue='';}});
  }
  function destroyEditor(){editor?.destroy();editor=null;}
  function readDraft(){
    if(editor)return editor.getMarkdown();
    const textarea=dialog.querySelector('.skill-source-editor');if(!textarea)return draft;
    if(textarea.value===textarea._skillOriginalValue)return textarea._skillOriginalSource;
    return textarea.value.replace(/\r?\n/g,textarea._skillOriginalSource.includes('\r\n')?'\r\n':'\n');
  }
  function hasChanges(){return !!current&&editing&&readDraft()!==current.files[selected];}
  function allowDiscard(){return !saving&&(!hasChanges()||confirm('当前文件有未保存的修改，确定放弃吗？'));}
  function requestClose(){if(allowDiscard())dialog.close();}
  function setStatus(text,error=false){
    const status=dialog.querySelector('[data-skill-edit-status]');status.textContent=text;status.classList.toggle('error',error);
    dialog.querySelector('.skill-editor-bar').hidden=!editing&&!text;
  }
  function updateEditing(){
    const dirty=hasChanges();
    dialog.querySelector('[data-skill-save]').disabled=saving||!dirty;
    if(editing)setStatus(saving?'正在保存…':dirty?'有未保存的修改':'可直接点击正文编辑，尚未修改');
  }
  function setSaving(value){
    saving=value;
    dialog.querySelector('.skill-browser-content').inert=value;
    dialog.querySelectorAll('[data-skill-close],[data-skill-edit],[data-skill-source],[data-skill-cancel],[data-skill-format],[data-skill-add-paragraph],[data-skill-tree] button').forEach(button=>button.disabled=value);
    dialog.querySelector('[data-skill-save]').textContent=value?'正在保存…':'保存修改';
    updateEditing();
  }
  async function saveFile(){
    if(!current||saving||!hasChanges())return;
    draft=readDraft();setSaving(true);let failure='',saved=false;
    try{
      const result=await api(`/api/resources/skills/${encodeURIComponent(current.skill_id)}/file`,{method:'PUT',body:JSON.stringify({path:selected,content:draft,expected_version:current.version})});
      current.version=result.version;current.files[selected]=draft;legacy=false;editing=false;draft='';renderFile();
      setStatus(`已保存 · 配置版本 ${result.version}`);saved=true;
    }catch(error){failure=`保存失败：${error.message}。草稿已保留，可切换源码复制。`;}
    finally{setSaving(false);if(failure)setStatus(failure,true);}
    if(saved)try{await onSaved?.();}catch{setStatus('文件已保存，目录刷新失败，请点击刷新。',true);}
  }
  function renderRelativeLinks(container) {
    container.querySelectorAll('a').forEach(link=>{
      const href=link.getAttribute('href')||'',path=resolveFile(href,selected,current.files);
      if(path){link.dataset.skillFile=path;link.setAttribute('href','#');}
      else if(/^https?:\/\//i.test(href)){link.target='_blank';link.rel='noreferrer';}
      else link.replaceWith(document.createTextNode(link.textContent));
    });
    const walker=document.createTreeWalker(container,NodeFilter.SHOW_TEXT),nodes=[];
    while(walker.nextNode())if(!walker.currentNode.parentElement.closest('pre,code,a'))nodes.push(walker.currentNode);
    for(const node of nodes){
      const pattern=/\[([^\]]+)\]\(([^)\s]+)\)/g;let match,last=0,changed=false;const fragment=document.createDocumentFragment();
      while((match=pattern.exec(node.textContent))){
        const path=resolveFile(match[2],selected,current.files);if(!path)continue;
        fragment.append(document.createTextNode(node.textContent.slice(last,match.index)));
        const link=document.createElement('button');link.type='button';link.className='skill-file-link';link.dataset.skillFile=path;link.textContent=match[1];fragment.append(link);
        last=pattern.lastIndex;changed=true;
      }
      if(changed){fragment.append(document.createTextNode(node.textContent.slice(last)));node.replaceWith(fragment);}
    }
  }
  function renderFile() {
    if(!current)return;
    destroyEditor();
    const content=editing?draft:current.files[selected]||'',markdown=/\.md$/i.test(selected),preview=dialog.querySelector('.skill-browser-content');
    dialog.querySelector('[data-skill-current-path]').textContent=current.skill_id+'/'+selected;
    dialog.querySelector('[data-skill-file-summary]').textContent=`${Object.keys(current.files).length} 个文本文件${legacy?' · 历史技能仅保存了合并正文':''}`;
    const button=dialog.querySelector('[data-skill-source]');button.hidden=!markdown;button.textContent=editing?(raw?'切换渲染编辑':'切换源码'):(raw?'查看预览':'查看源文件');button.setAttribute('aria-pressed',String(raw));
    dialog.querySelector('[data-skill-write]').hidden=editing||!markdown;
    dialog.querySelector('[data-skill-cancel]').hidden=!editing;dialog.querySelector('[data-skill-save]').hidden=!editing;
    dialog.querySelector('.skill-editor-format').hidden=!editing||raw;
    setStatus('');
    dialog.querySelectorAll('[data-skill-tree] [data-skill-file]').forEach(el=>{
      const active=el.dataset.skillFile===selected;el.setAttribute('aria-current',String(active));
      if(active)for(let parent=el.parentElement;parent&&parent!==dialog;parent=parent.parentElement)if(parent.tagName==='DETAILS')parent.open=true;
    });
    preview.classList.toggle('markdown-body',markdown&&!raw);preview.classList.toggle('skill-writing',editing&&!raw);
    if(editing&&raw){
      const textarea=document.createElement('textarea');textarea.className='skill-source-editor';textarea.setAttribute('aria-label','编辑 Markdown 源码');textarea.spellcheck=false;textarea.value=content;textarea._skillOriginalSource=content;textarea._skillOriginalValue=textarea.value;textarea.oninput=updateEditing;preview.replaceChildren(textarea);
    }else if(editing){editor=SkillMarkdownEditor.mount(preview,content,updateEditing);}
    else if(markdown&&!raw){
      SkillMarkdownEditor.render(preview,content);renderRelativeLinks(preview);
    }else {const pre=document.createElement('pre');pre.textContent=content;preview.replaceChildren(pre);}
    preview.scrollTop=0;updateEditing();
  }
  function open(data,onEdit,changed) {
    mount();if(current&&!allowDiscard())return;
    destroyEditor();
    const files=data.files&&Object.keys(data.files).length?{...data.files}:{'SKILL.md':data.body||''};
    current={...data,files};selected=Object.prototype.hasOwnProperty.call(files,'SKILL.md')?'SKILL.md':Object.keys(files)[0];raw=false;editing=false;draft='';legacy=!data.files;onSaved=changed;editConfig=onEdit;
    dialog.querySelector('#skill-browser-title').textContent=data.skill_id;
    dialog.querySelector('[data-skill-root]').textContent=data.skill_id+'/';
    dialog.querySelector('[data-skill-tree]').innerHTML=fileTree(files);
    renderFile();if(!dialog.open)dialog.showModal();
  }
  return {open};
})();
