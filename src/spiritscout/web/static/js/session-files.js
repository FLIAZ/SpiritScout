"use strict";
/* 报告索引独立于会话文本；保留未变化的卡片与滚动位置，请求按会话/代次隔离。 */
const SessionFiles = (() => {
  const statusLabels={done:I18n.t("已完成"),failed:I18n.t("执行失败"),cancelled:I18n.t("已撤销"),pruned:I18n.t("已暂缓")};
  let session="", generation=0, recovery=0, active=false, lastLoad=0, pending=false, items=[];
  const rows=new Map();
  const root=()=>$("#session-files");
  function reset(id) {
    session=id;generation++;recovery++;active=false;lastLoad=0;pending=false;items=[];rows.clear();
    root().hidden=true;document.querySelector('[data-tab="files"]').hidden=true;
    root().querySelector("[data-files-list]").replaceChildren();
    root().querySelector("[data-files-search]").value="";
    root().querySelector("[data-files-error]").hidden=true;
    root().querySelector("[data-files-count]").textContent="";
    root().querySelector("[data-files-status]").textContent=I18n.t("正在同步报告文件…");
    root().querySelector("[data-files-recover]").disabled=false;
    root().querySelector("[data-files-refresh]").disabled=false;
  }
  function sync(value) {
    if(session!==S.cur)reset(S.cur);
    active=value?.mode==="src" && !value?.side_session;
    root().hidden=!active;document.querySelector('[data-tab="files"]').hidden=!active;
    if(active)refresh();
  }
  function render() {
    const list=root().querySelector("[data-files-list]");
    const query=root().querySelector("[data-files-search]").value.trim().toLocaleLowerCase();
    const top=list.scrollTop;
    const paths=new Set(items.map(item=>item.path));
    for(const [path,row] of rows)if(!paths.has(path)){row.remove();rows.delete(path);}
    let anchor=list.firstElementChild;
    for(const item of items) {
      let row=rows.get(item.path);
      if(!row){row=document.createElement("article");row.className="session-file";rows.set(item.path,row);}
      const signature=JSON.stringify(item);
      if(row.dataset.signature!==signature){
        row.dataset.signature=signature;
        row.innerHTML=`<strong class="session-file-title">${esc(item.title)}</strong>`+
          `<small>${esc(actorName(item.actor) || item.actor)} · ${esc(item.task_id)} · ${esc(statusLabels[item.status] || item.status)}</small>`+
          FileCards.card({rel:item.path,name:item.name,size:item.size});
      }
      row.hidden=!!query && ![item.title,item.actor,item.task_id,item.name].join(" ").toLocaleLowerCase().includes(query);
      // 顺序稳定时不移动现有节点，保持键盘焦点和列表滚动。
      if(row!==anchor)list.insertBefore(row,anchor);
      anchor=row.nextElementSibling;
    }
    list.scrollTop=top;
    root().querySelector("[data-files-count]").textContent=I18n.t`${items.length} 份`;
    root().querySelector("[data-files-status]").textContent=items.length ? I18n.t("完整报告保存在本会话，点击预览或下载。") : I18n.t("子 Agent 收尾后报告会出现在这里；旧会话可补齐历史报告。");
  }
  async function refresh(force=false) {
    if(!active || S.cur!==session || S.tab!=="files" || (pending && !force) || (!force && Date.now()-lastLoad<10000))return;
    const id=session, current=++generation;
    pending=true;root().querySelector("[data-files-refresh]").disabled=true;
    root().querySelector("[data-files-error]").hidden=true;
    try {
      const page=await api(`/api/sessions/${encodeURIComponent(id)}/task-reports`);
      if(id!==session || S.cur!==id || current!==generation)return;
      items=page.items;lastLoad=Date.now();render();
    } catch(error) {
      if(id!==session || S.cur!==id || current!==generation)return;
      const box=root().querySelector("[data-files-error]");box.textContent=error.message;box.hidden=false;
    } finally {
      if(id===session && S.cur===id && current===generation){pending=false;root().querySelector("[data-files-refresh]").disabled=false;}
    }
  }
  async function recover() {
    const id=session, current=++recovery, button=root().querySelector("[data-files-recover]");
    button.disabled=true;
    try {
      await api(`/api/sessions/${encodeURIComponent(id)}/task-reports/recover`,{method:"POST"});
      if(id!==session || S.cur!==id || current!==recovery)return;
      await refresh(true);
    } catch(error) {
      if(id!==session || S.cur!==id || current!==recovery)return;
      const box=root().querySelector("[data-files-error]");box.textContent=error.message;box.hidden=false;
    } finally {if(id===session && S.cur===id && current===recovery)button.disabled=false;}
  }
  document.addEventListener("DOMContentLoaded",()=>{
    root().querySelector("[data-files-search]").addEventListener("input",render);
    root().querySelector("[data-files-refresh]").addEventListener("click",()=>refresh(true));
    root().querySelector("[data-files-recover]").addEventListener("click",recover);
  });
  return {reset,sync,refresh};
})();
