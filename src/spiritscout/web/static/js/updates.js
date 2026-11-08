"use strict";
/* 版本提醒保持安静；只有用户打开详情时才展示网络与安装限制。 */
const AppUpdates = (() => {
  let state = null, loading = false, applying = false, requested = false, timer = null;
  const phases = new Set(["preparing", "backup", "switching", "restarting"]);
  const dlg = () => $("#update-dlg");
  function render() {
    if (!state) return;
    UpdateGuides.render(state);
    const icon = $("#btn-update"), active = phases.has(state.job?.phase);
    icon.hidden = !state.available && !active && state.job?.phase !== "recovery_required";
    icon.title = active ? I18n.t("正在更新") : state.job?.phase === "recovery_required" ? I18n.t("更新需要处理") : I18n.t`可以更新到 ${state.release?.tag || I18n.t("新版本")}`;
    icon.setAttribute("aria-label", icon.title);
    $("#update-summary").textContent = I18n.t`当前 v${state.current} · ${I18n.t(state.installation.label)}`;
    $("#update-target").textContent = state.available ? I18n.t`发现新版本 ${state.release.tag}` : state.error ? I18n.t("暂时无法确认最新版本") : I18n.t("当前没有更新版本");
    const phase = state.job?.phase;
    const status = $("#update-status");
    status.textContent = state.job?.message && (active || ["error", "recovery_required"].includes(phase))
      ? I18n.t(state.job.message) : I18n.t(state.error || state.reason || "") || I18n.t("更新前会备份配置和工作区。更新完成后服务会重启，页面将自动重新连接。");
    status.classList.toggle("update-error", ["error", "recovery_required"].includes(phase));
    $("#update-steps").hidden = !active;
    const order = ["preparing", "backup", "switching", "restarting"];
    $("#update-steps").querySelectorAll("li").forEach((el, index) => {
      el.classList.toggle("current", index === order.indexOf(phase));
      el.classList.toggle("done", index < order.indexOf(phase));
    });
    $("#update-apply").disabled = !state.can_update || applying;
    $("#update-apply").textContent = active ? I18n.t("正在更新…") : I18n.t("更新并重启");
    $("#update-check").disabled = active || loading;
    const log = $("#update-changelog");
    const text = state.release?.changelog || I18n.t("发布更新后，这里会展示对应版本的更新日志。");
    const logKey = (state.release?.commit || "none") + (typeof state.release?.changelog === "string" ? "-full" : "-summary");
    if (log.dataset.commit !== logKey) {
      log.innerHTML = mdRender(text);
      log.dataset.commit = logKey;
    }
    $("#update-log-source").textContent = state.release ? `${state.release.tag} · ${state.release.changelog_source}` : I18n.t("更新日志");
    const details = $("#update-manual");
    details.hidden = !["container", "system_package", "appimage"].includes(state.installation.kind);
    details.textContent = state.installation.kind === "container"
      ? state.installation.supported ? I18n.t("宿主更新助手已连接，将由宿主构建镜像、备份并重建容器。") : I18n.t("Docker：请在宿主启动 scripts/update-container.py 连接本控制台以启用网页更新，也可以按原部署方式重新构建容器。")
      : I18n.t("此安装由发行包管理，请通过原安装渠道更新。应用内不会覆盖包管理器所属文件。");
    $("#update-recovery").hidden = phase !== "recovery_required";
    if (phase === "recovery_required") $("#update-recovery").textContent = I18n.t`请检查本机更新目录中的 jobs/${state.job.id || ""}/worker.log、service.log 和 backup/。新版可能已迁移数据，请按恢复说明处理。`;
    if (requested && phase === "completed") {
      requested = false;
      location.reload();
    }
  }
  async function refresh(force = false) {
    if (loading) return;
    loading = true;
    try {
      state = await api(force ? "/api/updates/check" : "/api/updates" + (dlg().open ? "?details=true" : ""), force ? {method: "POST"} : {});
      render();
    } catch (error) {
      if (dlg().open) $("#update-status").textContent = requested
        ? I18n.t("正在等待服务重新连接…如果持续无法连接，请检查本机更新日志。") : I18n.t`更新状态读取失败：${error.message}`;
    } finally {
      loading = false;
      if (state) $("#update-check").disabled = phases.has(state.job?.phase);
      schedule();
    }
  }
  function schedule() {
    clearTimeout(timer);
    const active = requested || phases.has(state?.job?.phase);
    timer = setTimeout(() => {
      if (document.visibilityState === "visible" || active) refresh(); else schedule();
    }, active ? 1500 : dlg().open ? 10000 : 60000);
  }
  async function open() {
    if (!dlg().open) dlg().showModal();
    await refresh();
  }
  async function apply() {
    if (applying || !state?.can_update) return;
    applying = true;
    render();
    let failure = "";
    try {
      await api("/api/updates/apply", {method: "POST", body: JSON.stringify({tag: state.release.tag})});
      requested = true;
    } catch (error) {
      failure = error.message;
    } finally {
      applying = false;
      await refresh();
      if (failure) $("#update-status").textContent = failure;
    }
  }
  function init() {
    UpdateGuides.render({installation:{kind:"unknown"}});
    $("#btn-update").onclick = open;
    $("#ver").onclick = open;
    $("#ver").onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } };
    $("#update-close").onclick = () => dlg().close();
    $("#update-check").onclick = () => refresh(true);
    $("#update-apply").onclick = apply;
    dlg().addEventListener("close", schedule);
    refresh();
  }
  return {init};
})();
AppUpdates.init();
