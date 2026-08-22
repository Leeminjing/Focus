/* 输入为主进程注入的启动阶段，输出为可访问文案与确定性进度；不读取 Node 或业务数据。 */
"use strict";

window.setFocusSplashStage = (label, progress = 0) => {
  const status = document.querySelector("#splashStatus");
  const bar = document.querySelector("#splashProgress");
  if (status) status.textContent = String(label || "正在启动 Focus");
  if (bar) bar.style.setProperty("--splash-progress", `${Math.max(0, Math.min(100, Number(progress) || 0))}%`);
};
