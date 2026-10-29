"use strict";
/* 分块编辑：未改动块原样回写，避免整篇 HTML 反转导致元数据、格式或引用丢失。 */
const SkillMarkdownEditor = (() => {
  const lineText=line=>line.replace(/\r?\n$/, '');
  const listPattern=/^(\s*)([-+*]|\d+[.)])\s+(.*)$/;
  function tableCells(line) {
    const text=lineText(line).trim().replace(/^\|/,'').replace(/\|$/,'');
    const cells=[];let cell='',escaped=false;
    for(const char of text){
      if(char==='|'&&!escaped){cells.push(cell.trim());cell='';}
      else cell+=char;
      escaped=char==='\\'&&!escaped;
    }
    cells.push(cell.trim());return cells;
  }
  function parse(source) {
    const front=source.match(/^---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/),prefix=front?.[0]||'';
    const lines=source.slice(prefix.length).match(/[^\n]*\n|[^\n]+$/g)||[],blocks=[];
    const special=line=>/^\s*$/.test(line)||/^ {0,3}(?:#{1,6}\s|`{3,}|~{3,}|>|\|)/.test(line)||listPattern.test(line)||/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line);
    for(let i=0;i<lines.length;){
      const start=i,text=lineText(lines[i]),fence=text.match(/^ {0,3}(`{3,}|~{3,})(.*)$/);let block;
      if(!text.trim()){i++;block={kind:'blank'};}
      else if(fence){
        i++;const bodyStart=i;
        const closing=new RegExp('^ {0,3}'+fence[1][0]+'{'+fence[1].length+',}\\s*$');
        while(i<lines.length&&!closing.test(lineText(lines[i])))i++;
        block={kind:'code',code:lines.slice(bodyStart,i).join(''),fence:fence[1],info:fence[2],closed:i<lines.length};if(i<lines.length)i++;
      }else if(/^ {0,3}#{1,6}\s+/.test(text)){
        const heading=text.match(/^( {0,3}#{1,6}\s+)(.*)$/);i++;
        block={kind:'heading',prefix:heading[1],level:heading[1].trim().length,text:heading[2]};
      }else if(/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(text)){i++;block={kind:'rule'};}
      else if(/^\s*\|/.test(text)){
        i++;while(i<lines.length&&/^\s*\|/.test(lineText(lines[i])))i++;
        block={kind:'table',rows:lines.slice(start,i).map(raw=>({raw,cells:tableCells(raw)}))};
      }else if(listPattern.test(text)){
        const match=text.match(listPattern);i++;
        while(i<lines.length&&/^\s{2,}\S/.test(lines[i])&&!special(lineText(lines[i])))i++;
        block={kind:'list',indent:match[1],marker:match[2],text:[match[3],...lines.slice(start+1,i).map(line=>lineText(line).trimStart())].join('\n')};
      }else if(/^\s*>/.test(text)){
        i++;while(i<lines.length&&/^\s*>/.test(lines[i]))i++;
        block={kind:'quote',text:lines.slice(start,i).map(line=>lineText(line).replace(/^\s*> ?/,'')).join('\n')};
      }else {
        i++;while(i<lines.length&&!special(lineText(lines[i])))i++;
        block={kind:'paragraph',text:lines.slice(start,i).map(lineText).join('\n')};
      }
      block.raw=lines.slice(start,i).join('');blocks.push(block);
    }
    return {prefix,blocks,eol:source.includes('\r\n')?'\r\n':'\n'};
  }
  function safeLink(value) {
    return !/[\x00-\x20<>]/.test(value)&&!value.startsWith('//')&&(!/^[a-z][\w+.-]*:/i.test(value)||/^https?:\/\//i.test(value));
  }
  function inlineHtml(text) {
    const tokens=[];let marker='\uE000';while(text.includes(marker))marker+='\uE000';
    const hold=html=>{const key=marker+tokens.length+'\uE001';tokens.push(html);return key;};
    text=text.replace(/(`+)([^\n]*?)\1/g,(_,ticks,code)=>hold(`<code>${esc(code)}</code>`));
    text=text.replace(/\\([\\`*_[\]~|])/g,(_,char)=>hold(esc(char)));
    text=text.replace(/!\[([^\]\n]*)\]\(([^\n)]+)\)/g,whole=>hold(`<span class="skill-md-literal" contenteditable="false" data-skill-md-literal="${esc(whole)}">${esc(whole)}</span>`));
    text=text.replace(/(?<!!)\[([^\]\n]+)\]\(([^\s)]+)\)/g,(whole,label,url)=>safeLink(url)?hold(`<a href="${esc(url)}">${esc(label)}</a>`):whole);
    text=esc(text).replace(/\*\*(.+?)\*\*|__(.+?)__/g,(_,a,b)=>`<strong>${a||b}</strong>`)
      .replace(/(?<!\*)\*([^*]+)\*(?!\*)|(?<!_)_([^_]+)_(?!_)/g,(_,a,b)=>`<em>${a||b}</em>`)
      .replace(/~~(.+?)~~/g,'<s>$1</s>').replace(/\n/g,'<br>');
    const tokenPattern=new RegExp(marker+'(\\d+)\uE001','g');
    const expand=index=>tokens[+index].replace(tokenPattern,(_,child)=>expand(child));
    return text.replace(tokenPattern,(_,index)=>expand(index));
  }
  function inlineMarkdown(node) {
    if(node.nodeType===3)return node.textContent.replace(/\\/g,'\\\\').replace(/([`*_\[\]~])/g,'\\$1');
    const tag=node.tagName?.toLowerCase();
    if(tag==='br')return '\n';
    if(tag==='span'&&node.hasAttribute?.('data-skill-md-literal'))return node.getAttribute('data-skill-md-literal');
    if(tag==='code'){
      const text=node.textContent,ticks='`'.repeat(Math.max(1,...(text.match(/`+/g)||[]).map(value=>value.length+1)));
      const pad=/^`|`$|^ | $/.test(text)?' ':'';return ticks+pad+text+pad+ticks;
    }
    const content=Array.from(node.childNodes||[]).map(inlineMarkdown).join('');
    if(tag==='b'||tag==='strong')return `**${content}**`;
    if(tag==='i'||tag==='em')return `*${content}*`;
    if(tag==='s'||tag==='strike'||tag==='del')return `~~${content}~~`;
    if(tag==='a'){const href=node.getAttribute('href')||'';return safeLink(href)?`[${content}](${href})`:content;}
    if(tag==='div')return '\n'+content+'\n';
    if(tag==='p')return content+'\n';
    return content;
  }
  function serialize(block,eol) {
    if(!block.dirty||!block.element||block.element.innerHTML===block.initial)return block.raw;
    const tail=/\r?\n$/.test(block.raw)?eol:'';
    let text;
    if(block.kind==='code'){
      const code=block.element.querySelector('pre').textContent;
      if(code===block.code)return block.raw;
      const runs=code.match(new RegExp(block.fence[0]+'+','g'))||[];
      const fence=block.fence[0].repeat(Math.max(block.fence.length,...runs.map(run=>run.length+1)));
      return fence+block.info+eol+code.replace(/\r?\n/g,eol)+(code.endsWith('\n')?'':eol)+fence+tail;
    }
    if(block.kind==='table'){
      const rows=Array.from(block.element.querySelectorAll('tr'));let index=0;
      return block.rows.map(row=>{
        if(row.cells.every(cell=>/^:?-{2,}:?$/.test(cell)))return row.raw;
        const tr=rows[index++];if(!row.dirty||tr.innerHTML===row.initial)return row.raw;
        return '| '+Array.from(tr.children).map(cell=>inlineMarkdown(cell).replace(/\n+/g,' ').replace(/\|/g,'\\|')).join(' | ')+' |'+(/\r?\n$/.test(row.raw)?eol:'');
      }).join('');
    }
    if(block.kind==='list'){
      const items=Array.from(block.element.querySelectorAll('li')),ordered=/^\d/.test(block.marker),start=parseInt(block.marker,10);
      return items.map((item,index)=>{
        const value=inlineMarkdown(item).replace(/\n+$/,'');if(!value)return '';
        const marker=ordered?String(start+index)+block.marker.slice(-1):block.marker;
        return block.indent+marker+' '+value.replace(/\n/g,eol+block.indent+'  ');
      }).filter(Boolean).join(eol)+tail;
    }
    text=Array.from(block.element.childNodes).map(inlineMarkdown).join('').replace(/^\n|\n+$/g,'');
    if(!text)return '';
    if(block.kind==='heading')text=block.prefix+text;
    if(block.kind==='quote')text=text.split('\n').map(line=>'> '+line).join('\n');
    return text.replace(/\n/g,eol)+tail;
  }
  function mount(container,source,onChange,{readOnly=false}={}) {
    const documentState=parse(source),doc=container.ownerDocument;
    container.replaceChildren();
    function editable(element,label){
      if(readOnly)return element;
      element.contentEditable='true';element.setAttribute('role','textbox');element.setAttribute('aria-multiline','true');element.setAttribute('aria-label',label);element.spellcheck=false;
      return element;
    }
    function addElement(block){
      if(block.kind==='blank')return;
      const wrap=doc.createElement('div');wrap.className='skill-md-block';block.element=wrap;wrap._skillMdBlock=block;
      if(block.kind==='rule')wrap.append(doc.createElement('hr'));
      else if(block.kind==='code'){
        const pre=editable(doc.createElement('pre'),'编辑代码块');pre.textContent=block.code;wrap.append(pre);
      }else if(block.kind==='table'){
        const table=doc.createElement('table'),body=doc.createElement('tbody');let header=true;
        for(const row of block.rows){
          if(row.cells.every(cell=>/^:?-{2,}:?$/.test(cell)))continue;
          const tr=doc.createElement('tr');
          for(const value of row.cells){const cell=editable(doc.createElement(header?'th':'td'),'编辑表格单元格');cell.innerHTML=inlineHtml(value.replace(/\\\|/g,'|'));tr.append(cell);}
          row.initial=tr.innerHTML;row.element=tr;body.append(tr);header=false;
        }
        table.append(body);wrap.append(table);
      }else if(block.kind==='list'){
        const ordered=/^\d/.test(block.marker),list=doc.createElement(ordered?'ol':'ul');if(ordered)list.start=parseInt(block.marker,10);
        list.style.marginLeft=(block.indent.length*.5)+'em';const li=editable(doc.createElement('li'),'编辑列表项');li.innerHTML=inlineHtml(block.text);list.append(li);wrap.append(list);
      }else {
        const tag=block.kind==='heading'?'h'+block.level:block.kind==='quote'?'blockquote':'p';
        block.editable=editable(doc.createElement(tag),block.kind==='heading'?'编辑标题':'编辑段落');
        block.editable.innerHTML=inlineHtml(block.text);wrap.append(block.editable);
      }
      block.initial=wrap.innerHTML;container.append(wrap);
    }
    documentState.blocks.forEach(addElement);
    if(!readOnly&&!container.querySelector('[contenteditable="true"]')){const block={kind:'paragraph',text:'',raw:''};documentState.blocks.push(block);addElement(block);}
    function mark(target){
      const element=target?.nodeType===3?target.parentElement:target,wrap=element?.closest?.('.skill-md-block');
      const block=wrap?._skillMdBlock;if(!block||!container.contains(wrap))return;
      block.dirty=true;const rowElement=element.closest('tr');if(rowElement)for(const row of block.rows||[])if(row.element===rowElement)row.dirty=true;
    }
    const input=event=>{mark(event.target);onChange();};
    const paste=event=>{
      const target=event.target.closest('[contenteditable="true"]');if(!target)return;
      event.preventDefault();const text=event.clipboardData.getData('text/plain');
      // 只插入文本，避免外部 HTML、图片和事件属性进入可编辑 DOM。
      doc.execCommand('insertText',false,/^(TD|TH)$/.test(target.tagName)?text.replace(/\r?\n/g,' '):text);mark(target);onChange();
    };
    const keydown=event=>{
      if(event.key!=='Enter'||event.isComposing)return;
      const target=event.target.closest('[contenteditable="true"]');if(!target||target.tagName==='PRE')return;
      event.preventDefault();if(/^(TD|TH)$/.test(target.tagName))return;
      const selection=doc.getSelection(),range=selection?.rangeCount?selection.getRangeAt(0):null;
      if(target.tagName==='LI'&&!event.shiftKey&&range&&target.contains(range.endContainer)){
        range.deleteContents();const suffix=range.cloneRange();suffix.setEnd(target,target.childNodes.length);
        const next=editable(doc.createElement('li'),'编辑列表项');next.append(suffix.extractContents());if(!next.childNodes.length)next.append(doc.createElement('br'));
        target.after(next);const caret=doc.createRange();caret.selectNodeContents(next);caret.collapse(true);selection.removeAllRanges();selection.addRange(caret);
      }else doc.execCommand('insertLineBreak');
      mark(target);onChange();
    };
    const drop=event=>event.preventDefault();
    if(!readOnly){container.addEventListener('input',input);container.addEventListener('paste',paste);container.addEventListener('keydown',keydown);container.addEventListener('drop',drop);}
    return {
      getMarkdown:()=>documentState.prefix+documentState.blocks.map(block=>serialize(block,documentState.eol)).join(''),
      command(name){
        const selection=doc.getSelection();if(!selection?.anchorNode||!container.contains(selection.anchorNode))return;
        const anchor=selection.anchorNode.nodeType===3?selection.anchorNode.parentElement:selection.anchorNode;if(anchor.closest('pre,code'))return;
        if(name==='bold'||name==='italic'){for(const block of documentState.blocks)if(block.element&&selection.containsNode(block.element,true)){block.dirty=true;for(const row of block.rows||[])if(row.element&&selection.containsNode(row.element,true))row.dirty=true;}doc.execCommand(name);mark(selection.anchorNode);onChange();}
      },
      addParagraph(){
        const block={kind:'paragraph',text:'',raw:''};
        documentState.blocks.push({kind:'blank',raw:documentState.eol+documentState.eol},block);addElement(block);block.editable.focus();onChange();
      },
      focus(){container.querySelector('[contenteditable="true"]')?.focus({preventScroll:true});},
      destroy(){container.removeEventListener('input',input);container.removeEventListener('paste',paste);container.removeEventListener('keydown',keydown);container.removeEventListener('drop',drop);},
    };
  }
  return {mount,render:(container,source)=>mount(container,source,()=>{},{readOnly:true}),parse,inlineHtml,inlineMarkdown,tableCells,safeLink};
})();
