"use strict";
const form = document.getElementById("login-form");
const userEl = document.getElementById("login-user");
const passEl = document.getElementById("login-pass");
const errEl = document.getElementById("login-error");
const submitEl = document.getElementById("login-submit");
const toggleEl = document.getElementById("login-pw-toggle");

function showError(message) {
  errEl.textContent = I18n.t(message);
  errEl.hidden = false;
}
function clearError() {
  errEl.textContent = "";
  errEl.hidden = true;
}

toggleEl.addEventListener("click", () => {
  const shown = passEl.type === "text";
  passEl.type = shown ? "password" : "text";
  toggleEl.textContent = shown ? I18n.t("显示") : I18n.t("隐藏");
  toggleEl.setAttribute("aria-pressed", String(!shown));
  toggleEl.setAttribute("aria-label", shown ? I18n.t("显示密码") : I18n.t("隐藏密码"));
  passEl.focus();
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearError();
  const username = userEl.value.trim();
  const password = passEl.value;
  if (!username || !password) {
    showError(I18n.t("请输入管理员用户名和密码"));
    return;
  }
  submitEl.disabled = true;
  submitEl.textContent = I18n.t("正在验证…");
  try {
    const response = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ username, password }),
    });
    if (response.ok) {
      location.replace(I18n.url("/"));
      return;
    }
    if (response.status === 401) {
      showError(I18n.t("用户名或密码错误"));
    } else if (response.status === 429) {
      showError(I18n.t("尝试过于频繁，请稍后再试"));
    } else {
      let message = I18n.t("登录失败，请稍后再试");
      try {
        const data = await response.json();
        if (data && typeof data.detail === "string") message = data.detail;
      } catch {}
      showError(message);
    }
    passEl.select();
  } catch {
    showError(I18n.t("无法连接控制台，请检查服务是否在运行"));
  } finally {
    submitEl.disabled = false;
    submitEl.textContent = I18n.t("进入控制台");
  }
});
