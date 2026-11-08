"use strict";
/* 仅翻译显式声明的界面文案。模板插值保持原文，不扫描运行中的会话或证据 DOM。 */
const I18n = (() => {
  const key="ss-language", catalog=typeof EN_MESSAGES==='undefined'?{}:EN_MESSAGES;
  const normalize=value=>value==='en'?'en':value==='zh-CN'?'zh-CN':null;
  const query=normalize(new URLSearchParams(globalThis.location?.search||'').get('lang'));
  let saved=null;
  try {saved=normalize(globalThis.localStorage?.getItem(key));if(query)globalThis.localStorage?.setItem(key,query);} catch {}
  const locale=query||saved||'zh-CN', english=locale==='en';
  const templates=new WeakMap(),strings=new Map();
  let switchHandler=()=>{};
  const han=/[\u3400-\u9fff]/;
  const escape=value=>value.replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const decode=value=>value.replace(/&(#x[\da-f]+|#\d+|amp|lt|gt|quot|apos|nbsp);/gi,(whole,entity)=>{
    if(entity[0]==='#'){const n=entity[1].toLowerCase()==='x'?parseInt(entity.slice(2),16):Number(entity.slice(1));return n>0&&n<=0x10ffff?String.fromCodePoint(n):whole;}
    return ({amp:'&',lt:'<',gt:'>',quot:'"',apos:"'",nbsp:'\u00a0'})[entity.toLowerCase()]||whole;
  });
  function phrase(value){
    if(!han.test(value))return value;
    const leading=value.match(/^\s*/)[0],trailing=value.match(/\s*$/)[0],slots=[];
    const source=value.trim().replace(/\uE000(\d+)\uE001/g,(_,id)=>{let index=slots.indexOf(id);if(index<0){index=slots.length;slots.push(id);}return `{${index}}`;});
    const translated=catalog[source];
    if(translated===undefined)return value;
    return leading+translated.replace(/\{(\d+)\}/g,(whole,id)=>slots[id]===undefined?whole:`\uE000${slots[id]}\uE001`)+trailing;
  }
  function markup(source){
    const translate=value=>escape(phrase(decode(value)));
    // 插值先替换为不含 HTML 的占位符，只有源码里的文本与可见属性进入词表。
    return source.split(/(<\/?[A-Za-z][^>]*>|<!--[\s\S]*?-->)/g).map(part=>{
      if(part.startsWith('<!--'))return part;
      if(/^<\/?[A-Za-z]/.test(part))return part.replace(/\b(title|placeholder|aria-label|alt)=("[^"]*"|'[^']*')/g,(_,name,quoted)=>`${name}=${quoted[0]}${translate(quoted.slice(1,-1))}${quoted[0]}`);
      return translate(part);
    }).join('');
  }
  function render(input,values,html){
    const template=Array.isArray(input);
    if(!english)return template?input.reduce((text,part,i)=>text+part+(i<values.length?String(values[i]):''),''):String(input??'');
    const cache=template?templates:strings;
    const cacheKey=template?input:`${html?'h':'t'}:${input}`;
    let compiled=cache.get(cacheKey);
    if(!compiled){
      const source=template?input.map((part,i)=>part+(i<values.length?`\uE000${i}\uE001`:'')).join(''):String(input??'');
      compiled=(html?markup(source):phrase(source)).split(/\uE000(\d+)\uE001/g);
      if(!template&&strings.size>=2048)strings.clear();
      cache.set(cacheKey,compiled);
    }
    return compiled.map((part,i)=>i%2?String(values[Number(part)]):part).join('');
  }
  const t=(input,...values)=>render(input,values,false),html=(input,...values)=>render(input,values,true);
  function url(path){const target=new URL(path,globalThis.location.href);target.searchParams.set('lang',locale);return target.pathname+target.search+target.hash;}
  function init(){
    document.documentElement.lang=locale;
    document.title=t(document.title);
    if(english){
      const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
      for(let node=walker.nextNode();node;node=walker.nextNode()){
        if(node.parentElement?.closest('script,style,[data-brand-zh]'))continue;
        if(han.test(node.nodeValue))node.nodeValue=t(node.nodeValue);
      }
      document.querySelectorAll('[title],[placeholder],[aria-label],[alt]').forEach(el=>{
        for(const attr of ['title','placeholder','aria-label','alt'])if(el.hasAttribute(attr))el.setAttribute(attr,t(el.getAttribute(attr)));
      });
    }
    document.querySelectorAll('[data-language-toggle]').forEach(button=>{
      const icon=button.dataset.languageToggle==='icon';
      if(!icon)button.textContent=english?'简体中文':'English';
      button.setAttribute('lang',icon?locale:english?'zh-CN':'en');
      button.title=english?'Switch to Simplified Chinese (reload page)':'切换到英文界面（重新载入页面）';
      button.setAttribute('aria-label',button.title);
      button.onclick=()=>{
        const next=english?'zh-CN':'en';
        try{localStorage.setItem(key,next);}catch{}
        const target=new URL(location.href);target.searchParams.set('lang',next);
        switchHandler(target);
        location.assign(target.href);
      };
    });
  }
  if(typeof document!=='undefined')init();
  return {t,html,locale,dateLocale:english?'en-US':'zh-CN',url,onSwitch:handler=>{switchHandler=handler;}};
})();
