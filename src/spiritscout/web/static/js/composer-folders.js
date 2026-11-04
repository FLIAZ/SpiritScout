"use strict";
/* 在拖放事件中取得读取句柄，递归枚举目录；文件保持原始格式，不打包。 */
const ComposerFolders = (() => {
  async function folder(root) {
    const entries = [];
    let size = 0;
    async function walk(entry, prefix = "") {
      if (!entry.name || /[\/\\\x00]/.test(entry.name) || [".", ".."].includes(entry.name))
        throw new Error("文件夹包含不合法的路径名称");
      const path = prefix + entry.name;
      if (entry.isDirectory) {
        entries.push({path, directory:true});
        const reader = entry.createReader();
        // readEntries 可能分批返回，必须读到空批次。
        while (true) {
          const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
          if (!batch.length) break;
          for (const child of batch) await walk(child, path + "/");
        }
      } else if (entry.isFile) {
        const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
        size += file.size;
        if (size > 50 * 1024 * 1024) throw new Error("单个文件夹总大小不能超过 50MB");
        entries.push({path, file});
      } else throw new Error("无法读取拖入的文件夹条目");
    }
    // 根目录名作为附件名，子条目使用相对根目录的路径。
    const reader = root.createReader();
    while (true) {
      const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
      if (!batch.length) break;
      for (const child of batch) await walk(child, "");
    }
    return {name:root.name, size, entries, directory:true};
  }
  async function collect(transfer) {
    const items = Array.from(transfer.items || []).filter(item => item.kind === "file");
    const sources = items.map(item => ({entry:item.webkitGetAsEntry?.(), file:item.getAsFile?.()}));
    if (!sources.length) return Array.from(transfer.files || []);
    const files = [];
    for (const {entry, file} of sources) {
      if (entry?.isDirectory) files.push(await folder(entry));
      else if (file) files.push(file);
      else if (entry?.isFile) files.push(await new Promise((resolve, reject) => entry.file(resolve, reject)));
      else throw new Error("浏览器无法读取该拖入项，请改用附件按钮选择文件");
    }
    return files;
  }
  return {collect};
})();
