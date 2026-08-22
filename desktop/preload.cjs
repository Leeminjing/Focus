/*
 * 本文件向隔离渲染器公开最小桌面桥。输入为主进程设置的动态 loopback API Origin、会话令牌
 * 和受限 IPC，输出为只读 runtime 快照、目录选择与经主进程校验的外链打开函数；不代理
 * HTTP/SSE 或跨域请求。
 */
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("focusDesktop", {
  runtime: () => ({
    apiBase: process.env.FOCUS_DESKTOP_API,
    session: process.env.FOCUS_DESKTOP_SESSION,
  }),
  selectWorkspace: () => ipcRenderer.invoke("focus:select-workspace"),
  openExternal: url => ipcRenderer.invoke("focus:open-external", url),
});
