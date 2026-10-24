/* 为所有桌面工作台提供独立于机器人面板、端口和 session 的恢复仓库。
 * 仅同源工作台主 frame 可访问；退出前请求 renderer flush 并等待磁盘队列。
 */
"use strict";
const path = require("node:path");
const { PatrolRecovery } = require("./patrol-recovery.cjs");
const prefix = "focus:workspace-patrol-recovery-";
class WorkspacePatrolRecoveryHost {
  constructor({ ipcMain, directory, byteLimit, getWorkbench, origin }) {
    this.repository = new PatrolRecovery(path.join(directory, "workbench"), byteLimit);
    this.getWorkbench = getWorkbench;
    const check = event => {
      const contents = getWorkbench()?.webContents;
      if (contents && event.sender === contents && event.senderFrame === contents.mainFrame) {
        const url = new URL(event.senderFrame.url);
        if (url.origin === origin && ["/desktop/", "/desktop/index.html"].includes(url.pathname)) return;
      }
      throw new Error("仅允许工作台主窗口访问输入恢复数据");
    };
    ipcMain.on(`${prefix}read`, event => {
      try { check(event); event.returnValue = { value: this.repository.read() }; this.owner = event.sender; }
      catch (error) { event.returnValue = { error: error.message }; }
    });
    // 仅页面卸载使用同步确认；主进程仍沿异步磁盘队列保存，平时输入不阻塞。
    ipcMain.on(`${prefix}checkpoint`, async (event, state) => {
      try { check(event); event.returnValue = { value: await this.repository.save(state) }; }
      catch (error) { event.returnValue = { error: error.message }; }
    });
    for (const [name, write] of Object.entries({ put: value => this.repository.put(value.id, value.bytes), save: value => this.repository.save(value) })) {
      ipcMain.handle(`${prefix}${name}`, async (event, value) => {
        try { check(event); return { value: await write(value) }; }
        catch (error) { return { error: error.message }; }
      });
    }
    ipcMain.handle(`${prefix}flushed`, (event, value) => {
      check(event);
      if (value?.token !== this.pending?.token) return false;
      this.pending.finish(value.error);
      return true;
    });
    this.serial = 0;
  }
  async flush() {
    const win = this.getWorkbench();
    if (win && !win.isDestroyed() && this.owner === win.webContents && !win.webContents.isLoading()) {
      if (!this.pending) {
        const token = ++this.serial;
        const promise = new Promise((resolve, reject) => {
          const timer = setTimeout(() => finish("等待工作台保存输入超时"), 5000);
          const finish = error => { clearTimeout(timer); this.pending = null; error ? reject(new Error(error)) : resolve(); };
          this.pending = { token, finish };
        });
        this.pending.promise = promise;
        win.webContents.send(`${prefix}flush`, token);
      }
      await this.pending?.promise;
    }
    await this.repository.writes;
  }
}
module.exports = { WorkspacePatrolRecoveryHost };
