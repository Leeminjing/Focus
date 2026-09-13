/*
 * 本文件向隔离渲染器公开最小桌面桥。输入为主进程设置的动态 loopback API Origin、会话令牌
 * 和受限 IPC，输出为只读 runtime 快照、目录选择、经主进程校验的外链打开与工作区内文件打开、
 * 按路径（绝对路径，或相对工作区根）的预览解析与字节读取、另存为写盘函数及整页缩放读写；
 * 不代理 HTTP/SSE 或跨域请求。
 */
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("focusDesktop", {
  runtime: () => ({
    apiBase: process.env.FOCUS_DESKTOP_API,
    session: process.env.FOCUS_DESKTOP_SESSION,
  }),
  selectWorkspace: () => ipcRenderer.invoke("focus:select-workspace"),
  openExternal: url => ipcRenderer.invoke("focus:open-external", url),
  openPath: (filePath, workspacePath) => ipcRenderer.invoke("focus:open-path", filePath, workspacePath),
  resolvePreviewPath: (filePath, workspacePath) => ipcRenderer.invoke("focus:resolve-preview-path", filePath, workspacePath),
  readPreviewBytes: (filePath, workspacePath) => ipcRenderer.invoke("focus:read-preview-bytes", filePath, workspacePath),
  saveBytes: (suggestedName, bytes) => ipcRenderer.invoke("focus:save-bytes", suggestedName, bytes),
  setZoomLevel: level => ipcRenderer.invoke("focus:set-zoom-level", level),
  getZoomLevel: () => ipcRenderer.invoke("focus:get-zoom-level"),
});
