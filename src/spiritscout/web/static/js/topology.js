/* 依照真实父边逐层向右展开；环边不参与布局，避免损坏的历史数据卡死画布。 */
const TopologyLayout = (() => {
  function layers(ids, preds) {
    const known = new Set(ids), depth = new Map(), visiting = new Set();
    const visit = id => {
      if (depth.has(id)) return depth.get(id);
      visiting.add(id);
      let d = 0;
      for (const parent of preds.get(id) || []) {
        if (known.has(parent) && !visiting.has(parent)) d = Math.max(d, visit(parent) + 1);
      }
      visiting.delete(id);
      depth.set(id, d);
      return d;
    };
    ids.forEach(visit);
    const columns = [];
    ids.forEach(id => (columns[depth.get(id)] ||= []).push(id));
    return {columns, depth};
  }

  function positions(columns, preds, heights, gap, pad) {
    const y = new Map();
    // 同一分支的后代对齐父节点；同列节点按实际高度避让。
    for (const column of columns) {
      let bottom = pad - gap;
      const wanted = id => {
        const parents = (preds.get(id) || []).filter(p => y.has(p));
        return parents.length ? parents.reduce((sum, p) => sum + y.get(p) + heights.get(p) / 2, 0)
          / parents.length - heights.get(id) / 2 : pad;
      };
      column.sort((a, b) => wanted(a) - wanted(b));
      for (const id of column) {
        const top = Math.max(pad, wanted(id), bottom + gap);
        y.set(id, top);
        bottom = top + heights.get(id);
      }
    }
    return {y, height: Math.max(pad, ...[...y].map(([id, top]) => top + heights.get(id))) + pad};
  }
  return {layers, positions};
})();
