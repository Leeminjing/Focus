/* 本文件提供紧凑桌面机器人的 DOM 交互入口。
 * 输入为鼠标移动、拖放内容和宿主状态，输出为受限位置/内容 IPC 与现有机器人素材状态。
 * 工作流为点击唤起面板，拖动超过阈值只调整位置，外部拖入内容转成有界字节后送预览；状态更新不主动聚焦。
 * 示例：将截图拖到机器人会调用 patrol.receive，不会调用 workspace input 提交 API。
 */
(async function () {
  "use strict";
  if (new URLSearchParams(location.search).get("surface") !== "robot") return;
  document.body.dataset.surface = "robot";
  const host = document.querySelector("#robot"), bridge = window.focusDesktop?.patrol;
  host.hidden = false;
  const label = document.querySelector("#robot-status"), button = document.querySelector("#robot-open");
  if (!bridge?.enabled) { label.textContent = "请从 Focus 桌面版启动"; return; }
  let currentWorkspaceId = "";
  const stateLabels = { idle: "点击打开 · 拖入材料", sending: "正在发送", accepted: "已受理", running: "工作中", waiting_user: "需要你处理", paused: "已暂停", stopped: "已停止", failed: "需要查看", disconnected: "连接中断", completed: "工作已完成" };
  function update(state) {
    currentWorkspaceId = state.workspaceId || "";
    label.textContent = stateLabels[state.state] || "点击打开";
    label.title = state.workspaceName || "绑定工作区";
    const asset = { sending: "loading", accepted: "loading", running: "working", waiting_user: "thinking", failed: "alert", disconnected: "alert", completed: "happy" }[state.state] || "idle";
    button.querySelector("img").src = `./assets/patrol-avatar/${asset}.png`;
  }
  bridge.onState(update);
  const ready = await bridge.ready();
  let bounds = ready.bounds, drag = null, moved = false;
  update(ready.state);
  button.addEventListener("pointerdown", event => {
    if (event.button !== 0) return;
    drag = { x: event.screenX, y: event.screenY, bounds }; moved = false;
    button.setPointerCapture(event.pointerId);
  });
  button.addEventListener("pointermove", event => {
    if (!drag) return;
    const x = event.screenX - drag.x, y = event.screenY - drag.y;
    if (!moved && Math.hypot(x, y) < 5) return;
    moved = true;
    void bridge.move({ x: drag.bounds.x + x, y: drag.bounds.y + y }).then(value => { bounds = value; }).catch(error => { label.textContent = error.message; });
  });
  button.addEventListener("pointerup", () => { drag = null; });
  button.addEventListener("pointercancel", () => { drag = null; });
  button.addEventListener("click", () => { if (!moved) void bridge.openPanel(); moved = false; });
  document.querySelector("#robot-hide").addEventListener("click", () => bridge.hideRobot());
  host.addEventListener("dragover", event => { event.preventDefault(); host.classList.add("is-dragging"); });
  host.addEventListener("dragleave", () => host.classList.remove("is-dragging"));
  host.addEventListener("drop", async event => {
    event.preventDefault(); host.classList.remove("is-dragging");
    const workspaceId = currentWorkspaceId;
    try {
      const data = FocusWorkspacePatrolMaterialPreview.captureTransfer(event.dataTransfer);
      const items = await FocusWorkspacePatrolMaterialPreview.readTransfer(data, ready.limits);
      if (items.length) await bridge.receive({ items, workspaceId });
    } catch (error) { label.textContent = error.message; }
  });
})().catch(error => { document.querySelector("#robot-status").textContent = error.message; });
