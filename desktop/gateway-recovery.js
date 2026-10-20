/* 原生 Gateway 失联提示独立于工作区挂载，重连/重启不重载页面或提交新请求。 */
(function (global) {
  "use strict";
  // 源文案在渲染时取当前语言；模板插值始终保留原文。
  function localize(source, ...values) {
    if (globalThis.FocusI18n?.text) return globalThis.FocusI18n.text(source, ...values);
    return typeof source === "string" ? source : source.reduce((out, part, i) => out + part + (i < values.length ? String(values[i]) : ""), "");
  }

  const gateway = global.focusDesktop?.gateway;
  if (!gateway || new URLSearchParams(global.location.search).get("surface") === "robot") return;
  const document = global.document;
  const banner = document.createElement("aside");
  banner.className = "gateway-recovery";
  banner.hidden = true;
  banner.setAttribute("aria-label", "Focus Gateway");
  const message = document.createElement("p");
  message.setAttribute("role", "status");
  const reconnect = document.createElement("button");
  const restart = document.createElement("button");
  reconnect.type = restart.type = "button";
  banner.append(message, reconnect, restart);
  const panel = document.querySelector(".panel-feedback-content");
  if (panel) { banner.classList.add("is-patrol"); panel.prepend(banner); }
  else document.body.append(banner);
  let revision = -1, disconnected = false, latest = { status: "starting" };
  function english() {
    try {
      return global.FocusI18n?.locale?.() === "en-US"
        || (!global.FocusI18n && global.focusDesktop.preferences?.getItem("focus-interface-language-v1") === "en-US");
    } catch { return false; }
  }
  function draw(value) {
    if (!value || value.revision < revision) return;
    revision = value.revision;
    latest = value;
    const ready = value.status === "ready";
    const busy = ["reconnecting", "restarting"].includes(value.status);
    banner.hidden = ready || value.status === "starting";
    reconnect.disabled = restart.disabled = busy;
    const en = english();
    reconnect.textContent = en ? "Reconnect" : localize("重新连接");
    restart.textContent = en ? "Restart service" : localize("重启服务");
    message.textContent = busy
      ? (en ? "Restoring the local service… Your work stays open." : localize("正在恢复本地服务…当前工作已保留。"))
      : (en ? "Local service disconnected. Your window and drafts are preserved." : localize("本地服务已断开，当前窗口和草稿已保留。"));
    if (!busy && value.error) message.textContent += ` ${value.error}`;
    if (ready && disconnected) {
      disconnected = false;
      document.dispatchEvent(new global.CustomEvent("focus:gateway-restored"));
    } else if (!ready && value.status !== "starting") disconnected = true;
  }
  async function recover(operation) {
    try { draw(await gateway[operation]()); }
    catch (error) { draw({ ...latest, status: "unavailable", error: error.message }); }
  }
  reconnect.addEventListener("click", () => { void recover("reconnect"); });
  restart.addEventListener("click", () => { void recover("restart"); });
  document.addEventListener("focus:languagechange", () => draw(latest));
  const unsubscribe = gateway.onStatus(draw);
  global.addEventListener("beforeunload", unsubscribe, { once: true });
  void gateway.status().then(draw).catch(error => draw({ status: "unavailable", revision: 0, error: error.message }));
})(window);
