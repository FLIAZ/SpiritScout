"use strict";
/* 只处理已净化的展示文本；数据、Markdown 解析和流式节奏仍由调用方负责。 */
const StreamText = (() => {
  const root = document.documentElement;
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
  const active = new Map();
  const chunk = node => node.nodeType === 1 && node.classList.contains("stream-text-chunk");
  const textLike = node => node.nodeType === 3 || chunk(node);
  function selected(node) {
    const selection = document.getSelection();
    if (!selection || selection.isCollapsed) return false;
    for (let i = 0; i < selection.rangeCount; i++) if (selection.getRangeAt(i).intersectsNode(node)) return true;
    return false;
  }
  function settle(span) {
    active.delete(span);
    // 选区中的节点不移动；下次正文更新时可继续把静态 span 当作文本处理。
    if (!span.parentNode || selected(span)) return;
    const previous = span.previousSibling;
    if (previous?.nodeType === 3) {
      previous.appendData(span.textContent); span.remove();
    } else span.replaceWith(...span.childNodes);
  }
  function stop() {
    for (const [span, animation] of [...active]) { animation.cancel(); settle(span); }
  }
  new MutationObserver(stop).observe(root, {attributes: true, attributeFilter: ["data-motion"]});
  reduced.addEventListener("change", stop);
  document.addEventListener("visibilitychange", () => { if (document.hidden) stop(); });

  function settings(target, animate) {
    if (!animate || !target.isConnected || document.hidden || reduced.matches || root.dataset.motion === "off") return null;
    const style = getComputedStyle(root);
    const duration = parseFloat(style.getPropertyValue("--motion-fast"));
    return Number.isFinite(duration) && duration > 0 ? {duration, easing: style.getPropertyValue("--motion-ease").trim()} : null;
  }
  function append(parent, text, before, animationOptions) {
    if (!text) return;
    if (!animationOptions || !text.trim() || parent.closest("pre,code,table,[hidden]")) {
      const previous = before ? before.previousSibling : parent.lastChild;
      if (previous?.nodeType === 3) previous.appendData(text);
      else parent.insertBefore(document.createTextNode(text), before);
      return;
    }
    const span = document.createElement("span");
    span.className = "stream-text-chunk"; span.textContent = text;
    parent.insertBefore(span, before);
    const animation = span.animate([{opacity: .35}, {opacity: 1}], animationOptions);
    active.set(span, animation);
    animation.finished.then(() => settle(span), () => settle(span));
  }
  function sync(parent, incoming, state) {
    let cursor = parent.firstChild;
    for (const next of [...incoming.childNodes]) {
      if (next.nodeType === 3) {
        const group = [];
        while (cursor && textLike(cursor)) { group.push(cursor); cursor = cursor.nextSibling; }
        const previous = group.map(node => node.textContent).join("");
        const skip = Math.min(state.skip, next.data.length); state.skip -= skip;
        if (next.data.startsWith(previous)) {
          const boundary = Math.max(previous.length, skip);
          append(parent, next.data.slice(previous.length, boundary), cursor, null);
          append(parent, next.data.slice(boundary), cursor, state.options);
        }
        else {
          // 公共正文被修正或 Markdown 边界变化时直接替换，不重播旧内容。
          for (const node of group) node.remove();
          append(parent, next.data, cursor, null);
        }
        continue;
      }
      if (cursor?.nodeType === 1 && cursor.nodeName === next.nodeName) {
        for (const attr of [...cursor.attributes]) if (!next.hasAttribute(attr.name)) cursor.removeAttribute(attr.name);
        for (const attr of [...next.attributes]) if (cursor.getAttribute(attr.name) !== attr.value) cursor.setAttribute(attr.name, attr.value);
        sync(cursor, next, state);
        cursor = cursor.nextSibling;
      } else if (cursor) {
        const old = cursor; cursor = cursor.nextSibling;
        state.skip = Math.max(0, state.skip - next.textContent.length);
        old.replaceWith(next);
      } else {
        parent.append(next);
        // 新块只给文本叶子加渐显，代码和表格不拆字、不改变布局。
        const walker = document.createTreeWalker(next, NodeFilter.SHOW_TEXT);
        const nodes = []; while (walker.nextNode()) nodes.push(walker.currentNode);
        for (const node of nodes) {
          const text = node.textContent, skip = Math.min(state.skip, text.length); state.skip -= skip;
          if (state.options && !node.parentElement.closest("pre,code,table")) {
            const holder = node.parentNode, before = node.nextSibling;
            node.remove(); append(holder, text.slice(0, skip), before, null);
            append(holder, text.slice(skip), before, state.options);
          }
        }
      }
    }
    while (cursor) { const old = cursor; cursor = cursor.nextSibling; old.remove(); }
  }
  function plain(target, text, {animate = false} = {}) {
    const incoming = document.createDocumentFragment(); incoming.append(document.createTextNode(text));
    sync(target, incoming, {skip: target.textContent.length, options: text.startsWith(target.textContent) ? settings(target, animate) : null});
  }
  function markdown(target, html, {animate = false} = {}) {
    const template = document.createElement("template"); template.innerHTML = html;
    const previous = target.textContent;
    sync(target, template.content, {skip: previous.length, options: template.content.textContent.startsWith(previous) ? settings(target, animate) : null});
  }
  return {plain, markdown};
})();
