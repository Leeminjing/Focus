/*
 * 本文件对外提供隔离渲染器的最小桌面桥。
 * 输入为同源 API、会话令牌、能力开关与受限 IPC；输出为 runtime、目录选择、外链、缩放及 Patrol 窗口白名单函数。
 * 工作流为 renderer 自行处理同源 HTTP/SSE，机器人将有界字节或文本转给唯一面板，回调只接收数据而不暴露 IPC event。
 * 示例：await window.focusDesktop.patrol.receive({items:[{kind:'text',text:'参考内容'}]})；File 必须先在 renderer 转成 Uint8Array。
 */
const { contextBridge, ipcRenderer } = require("electron");
const nativeMaterial = process.argv.includes("--focus-native-material=acrylic");
const patrolEnabled = process.argv.includes("--focus-desktop-patrol");
function subscribe(channel, callback) {
  if (typeof callback !== "function") throw new TypeError("需要回调函数");
  const listener = (_event, value) => callback(value);
  ipcRenderer.on(channel, listener);
  return () => ipcRenderer.removeListener(channel, listener);
}
function recoveryCall(operation, value) {
  const result = ipcRenderer.sendSync(`focus:patrol-recovery-${operation}`, value);
  if (result?.error) throw new Error(result.error);
  return result.value;
}
function mainRequestCall(operation, key, value) {
  const result = ipcRenderer.sendSync(`focus:main-request-${operation}`, key, value);
  if (result?.error) throw new Error(result.error);
  if (!result || !("value" in result)) throw new Error("普通任务恢复仓库不可用");
  return result.value;
}
async function recoveryWrite(operation, value) {
  const result = await ipcRenderer.invoke(`focus:patrol-recovery-${operation}`, value);
  if (result?.error) throw new Error(result.error);
  return result.value;
}
window.addEventListener("DOMContentLoaded", () => {
  if (nativeMaterial) document.documentElement.dataset.nativeMaterial = "acrylic";
}, { once: true });

contextBridge.exposeInMainWorld("focusDesktop", {
  mainRequestStorage: {
    getItem: key => mainRequestCall("getItem", key),
    setItem: (key, value) => mainRequestCall("setItem", key, value),
    removeItem: key => mainRequestCall("removeItem", key),
  },
  runtime: () => ({
    apiBase: process.env.FOCUS_DESKTOP_API,
    session: process.env.FOCUS_DESKTOP_SESSION,
    nativeMaterial,
    features: { liveLoopProjection: !["0", "false", "off", "no"].includes(String(process.env.FOCUS_LOOP_FRONTEND_PROJECTION || "true").toLowerCase()) },
  }),
  selectWorkspace: () => ipcRenderer.invoke("focus:select-workspace"),
  openExternal: url => ipcRenderer.invoke("focus:open-external", url),
  files: {
    open: reference => ipcRenderer.invoke("focus:file-open", reference),
    reveal: reference => ipcRenderer.invoke("focus:file-reveal", reference),
    saveAs: reference => ipcRenderer.invoke("focus:file-save-as", reference),
  },
  fileAction: request => ipcRenderer.invoke("focus:file-action", request),
  setZoomLevel: level => ipcRenderer.invoke("focus:set-zoom-level", level),
  getZoomLevel: () => ipcRenderer.invoke("focus:get-zoom-level"),
  notifications: {
    settings: enabled => ipcRenderer.invoke("focus:notifications-settings", enabled),
    ready: () => ipcRenderer.invoke("focus:notifications-ready"),
    onOpen: callback => subscribe("focus:notification-open", callback),
    onTasks: callback => subscribe("focus:tasks-updated", callback),
  },
  workspacePatrolRecovery: {
    checkpoint: state => {
      const result = ipcRenderer.sendSync("focus:workspace-patrol-recovery-checkpoint", state);
      if (result?.error) throw new Error(result.error);
      return result.value;
    },
    read: () => {
      const result = ipcRenderer.sendSync("focus:workspace-patrol-recovery-read");
      if (result?.error) throw new Error(result.error);
      return result.value;
    },
    put: async (id, bytes) => {
      const result = await ipcRenderer.invoke("focus:workspace-patrol-recovery-put", { id, bytes });
      if (result?.error) throw new Error(result.error);
      return result.value;
    },
    save: async state => {
      const result = await ipcRenderer.invoke("focus:workspace-patrol-recovery-save", state);
      if (result?.error) throw new Error(result.error);
      return result.value;
    },
    onFlush: callback => subscribe("focus:workspace-patrol-recovery-flush", callback),
    flushed: result => ipcRenderer.invoke("focus:workspace-patrol-recovery-flushed", result),
  },
  patrol: {
    enabled: patrolEnabled,
    recovery: {
      read: () => recoveryCall("read"),
      put: (id, bytes) => recoveryWrite("put", { id, bytes }),
      save: state => recoveryWrite("save", state),
      onFlush: callback => subscribe("focus:patrol-recovery-flush", callback),
      flushed: result => ipcRenderer.invoke("focus:patrol-recovery-flushed", result),
    },
    openPanel: target => ipcRenderer.invoke("focus:patrol-open", target),
    hidePanel: () => ipcRenderer.invoke("focus:patrol-hide-panel"),
    resizePanel: size => ipcRenderer.invoke("focus:patrol-resize-panel", size),
    hideRobot: () => ipcRenderer.invoke("focus:patrol-hide-robot"),
    openWorkbench: target => ipcRenderer.invoke("focus:patrol-workbench", target),
    receive: content => ipcRenderer.invoke("focus:patrol-receive", content),
    move: position => ipcRenderer.invoke("focus:patrol-move", position),
    setState: state => ipcRenderer.invoke("focus:patrol-state", state),
    ready: () => ipcRenderer.invoke("focus:patrol-ready"),
    onContent: callback => subscribe("focus:patrol-content", callback),
    onOpen: callback => subscribe("focus:patrol-open", callback),
    onVisibility: callback => subscribe("focus:patrol-visibility", callback),
    onState: callback => subscribe("focus:patrol-state", callback),
    onWorkbench: callback => subscribe("focus:patrol-open-workbench", callback),
  },
});
