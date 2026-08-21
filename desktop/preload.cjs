/*
 * 本文件向隔离渲染器公开最小桌面桥。输入为主进程设置的动态 loopback API Origin、会话令牌
 * 和目录选择 IPC，输出为只读 runtime 快照与目录选择函数；不代理 HTTP/SSE 或跨域请求。
 */
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("focusDesktop", {
  runtime: () => ({
    apiBase: process.env.FOCUS_DESKTOP_API,
    session: process.env.FOCUS_DESKTOP_SESSION,
  }),
  selectWorkspace: () => ipcRenderer.invoke("focus:select-workspace"),
});
