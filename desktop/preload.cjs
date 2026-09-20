/*
 * 本文件对外提供隔离渲染器的最小桌面桥。
 * 输入为主进程设置的 loopback API Origin、会话令牌、Live Loop 前端开关和受限 IPC；输出为只读 runtime、目录选择、外链与整页缩放函数。
 * 具体工作流为通过 contextBridge 暴露白名单方法，renderer 自行处理同源 HTTP/SSE，本文件不代理网络请求；示例：`window.focusDesktop.runtime()`。
 */
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("focusDesktop", {
  runtime: () => ({
    apiBase: process.env.FOCUS_DESKTOP_API,
    session: process.env.FOCUS_DESKTOP_SESSION,
    features: { liveLoopProjection: !["0", "false", "off", "no"].includes(String(process.env.FOCUS_LOOP_FRONTEND_PROJECTION || "true").toLowerCase()) },
  }),
  selectWorkspace: () => ipcRenderer.invoke("focus:select-workspace"),
  openExternal: url => ipcRenderer.invoke("focus:open-external", url),
  setZoomLevel: level => ipcRenderer.invoke("focus:set-zoom-level", level),
  getZoomLevel: () => ipcRenderer.invoke("focus:get-zoom-level"),
});
