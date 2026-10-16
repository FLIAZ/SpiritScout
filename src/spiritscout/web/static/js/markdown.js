"use strict";
/* 只渲染支持的 Markdown 子集，原始 HTML 始终转义。 */
function mdRender(md) {
  const escT = s => esc(s);
  const lines = md.split("\n");
  let html = "", inCode = false, inTable = false, list = 0;
  const inline = s => escT(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
  const closeList = () => { while (list) { html += "</ul>"; list--; } };
  for (const ln of lines) {
    if (/^```/.test(ln)) {
      if (inCode) html += "</pre>"; else { closeList(); html += "<pre>"; }
      inCode = !inCode; continue;
    }
    if (inCode) { html += escT(ln) + "\n"; continue; }
    const h = ln.match(/^(#{1,4})\s+(.*)/);
    if (h) { closeList(); const lv = h[1].length; html += `<h${lv}>${inline(h[2])}</h${lv}>`; continue; }
    if (/^\s*\|/.test(ln)) {          // 表格行：容忍缺尾 | 的行（模型产出常见）
      closeList();
      const cells = ln.trim().replace(/^\|/, "").replace(/\|\s*$/, "")
        .split("|").map(c => c.trim());
      if (cells.every(c => /^:?-{2,}:?$/.test(c))) continue;   // 分隔行不渲染
      if (!inTable) { html += "<table>"; inTable = true; }
      html += "<tr>" + cells.map(c => `<td>${inline(c)}</td>`).join("") + "</tr>";
      continue;
    }
    if (inTable) { html += "</table>"; inTable = false; }
    const li = ln.match(/^(\s*)[-*]\s+(.*)/);
    if (li) {
      const d = Math.floor(li[1].length / 2);
      while (list < d + 1) { html += "<ul>"; list++; }
      while (list > d + 1) { html += "</ul>"; list--; }
      html += `<li>${inline(li[2])}</li>`; continue;
    }
    closeList();
    if (/^---+\s*$/.test(ln)) { html += "<hr>"; continue; }
    if (ln.trim()) html += `<p>${inline(ln)}</p>`;
  }
  closeList();
  if (inCode) html += "</pre>";
  if (inTable) html += "</table>";
  return html;
}
