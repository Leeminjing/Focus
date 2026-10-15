/*
 * 本文件对外提供 DesktopPatrolHost、clampBounds、panelBounds 和 validateContent。
 * 输入为现有 Electron 运行时、同源 API、材料限制和工作台回调；输出为单一机器人、邻接面板和受限 IPC。
 * 工作流为恢复 userData 中的位置与工作区身份，建立同 session 的两个窗口，转交显式用户输入并保留面板，
 * 通过托盘恢复窗口或明确退出；仅面板持有草稿，宿主只缓存尚未交付的有界消息及退出提醒标志。
 * 示例：const host = new DesktopPatrolHost(options); await host.start(); host.restore();
 */
"use strict";

const fs = require("node:fs");
const path = require("node:path");

const ROBOT_SIZE = { width: 144, height: 164 };
const PANEL_SIZE = { width: 440, height: 620 };
const PREFIX = "focus:patrol-";

function clampBounds(bounds, area) {
  const width = Math.min(bounds.width, area.width);
  const height = Math.min(bounds.height, area.height);
  return {
    x: Math.max(area.x, Math.min(Math.round(bounds.x), area.x + area.width - width)),
    y: Math.max(area.y, Math.min(Math.round(bounds.y), area.y + area.height - height)),
    width, height,
  };
}

function panelBounds(robot, area) {
  const x = robot.x - PANEL_SIZE.width - 8 >= area.x
    ? robot.x - PANEL_SIZE.width - 8 : robot.x + robot.width + 8;
  return clampBounds({ x, y: robot.y + robot.height - PANEL_SIZE.height, ...PANEL_SIZE }, area);
}

function boundedString(value, limit, name, optional = false) {
  if (optional && (value === undefined || value === null)) return "";
  if (typeof value !== "string" || value.length > limit) throw new Error(`${name} 无效或过长`);
  return value;
}

function validateContent(value, limits) {
  if (!value || !Array.isArray(value.items) || !value.items.length || value.items.length > limits.run_material_count) {
    throw new Error("拖入材料数量无效或超出限制");
  }
  let totalBytes = 0;
  const items = value.items.map(item => {
    if (item?.kind === "text") {
      const text = boundedString(item.text, limits.general_file_bytes, "参考文字");
      if (!text.trim()) throw new Error("参考文字为空");
      totalBytes += Buffer.byteLength(text, "utf8");
      return { kind: "text", text };
    }
    if (item?.kind !== "file") throw new Error("仅支持文字、图片或文件，文件夹请绑定为工作区");
    const name = boundedString(item.name, 255, "文件名");
    const type = boundedString(item.type, 255, "文件类型", true);
    if (!name || /[\\/\u0000]/.test(name)) throw new Error("文件名不能包含路径");
    const bytes = item.bytes;
    if (!(bytes instanceof Uint8Array) || !bytes.byteLength) throw new Error("文件必须提供非空字节，不能传递路径或 File 对象");
    if (bytes.buffer.byteLength > limits.general_file_bytes) throw new Error("文件字节缓冲超出材料限制");
    totalBytes += bytes.byteLength;
    return { kind: "file", name, type, bytes };
  });
  if (totalBytes > limits.general_file_bytes) throw new Error("待预览内容总大小超出材料限制");
  return { items, totalBytes };
}

function validateTarget(value = {}) {
  const target = {};
  for (const key of ["workspaceId", "requestId", "taskId", "runId", "revisionId"]) {
    if (value[key] != null) target[key] = boundedString(value[key], 160, key);
  }
  if (value.requestRevision != null) {
    if (!Number.isSafeInteger(value.requestRevision) || value.requestRevision < 0) throw new Error("问题版本无效");
    target.requestRevision = value.requestRevision;
  }
  return target;
}

class DesktopPatrolHost {
  constructor({ electron, apiBase, desktopDir, icon, limits, openWorkbench, getWorkbench, quit }) {
    this._electron = electron;
    this._apiBase = apiBase;
    this._desktopDir = desktopDir;
    this._icon = icon;
    this._limits = limits;
    this._openWorkbench = openWorkbench;
    this._getWorkbench = getWorkbench;
    this._quit = quit;
    this._preferencesPath = path.join(electron.app.getPath("userData"), "desktop-patrol.json");
    this._state = { workspaceId: "", workspaceName: "", state: "idle", dirty: false, pending: false };
    this._queue = [];
    this._queuedBytes = 0;
    this._ready = new Set();
    this._handlers = [];
    this._disposed = false;
    this._quitConfirmed = false;
    this._quitPrompt = null;
    this._readPreferences();
  }

  async start() {
    const { BrowserWindow, screen, Tray, Menu } = this._electron;
    const area = screen.getPrimaryDisplay().workArea;
    const saved = this._position || { x: area.x + area.width - ROBOT_SIZE.width - 24, y: area.y + area.height - ROBOT_SIZE.height - 24 };
    const bounds = clampBounds({ ...saved, ...ROBOT_SIZE }, screen.getDisplayMatching({ ...saved, ...ROBOT_SIZE }).workArea);
    const common = {
      show: false, frame: false, resizable: false, maximizable: false, minimizable: false,
      skipTaskbar: true, alwaysOnTop: true, icon: this._icon,
      webPreferences: {
        preload: path.join(this._desktopDir, "preload.cjs"), contextIsolation: true,
        nodeIntegration: false, sandbox: true, backgroundThrottling: false,
        additionalArguments: ["--focus-desktop-patrol"],
      },
    };
    this.robot = new BrowserWindow({ ...common, ...bounds, transparent: true, backgroundColor: "#00000000", focusable: false });
    this.panel = new BrowserWindow({ ...common, ...panelBounds(bounds, screen.getDisplayMatching(bounds).workArea), backgroundColor: "#f4f7fb" });
    for (const [surface, win] of [["robot", this.robot], ["panel", this.panel]]) {
      this._protectWindow(win, surface);
      win.on("close", event => { if (!this._disposed) { event.preventDefault(); win.hide(); } });
      win.webContents.on("did-start-loading", () => this._ready.delete(surface));
    }
    this._onDisplaysChanged = () => this._reposition();
    for (const event of ["display-added", "display-removed", "display-metrics-changed"]) screen.on(event, this._onDisplaysChanged);
    this.robot.on("move", () => { this._positionPanel(); this._saveSoon(); });
    this._registerIpc();
    this._tray = new Tray(this._icon);
    this._tray.setToolTip("Focus · 桌面 Patrol");
    this._tray.setContextMenu(Menu.buildFromTemplate([
      { label: "显示机器人", click: () => this.restore() },
      { label: "打开工作台", click: () => this.openWorkbench() },
      { type: "separator" },
      { label: "退出 Focus", click: () => this.requestQuit() },
    ]));
    this._tray.on("double-click", () => this.restore());
    await Promise.all([
      this.robot.loadURL(this._url("robot")),
      this.panel.loadURL(this._url("panel")),
    ]);
    this.robot.showInactive();
  }

  accepts(event, roles = ["robot", "panel", "workbench"]) {
    if (!event || event.senderFrame !== event.sender?.mainFrame) return false;
    return roles.some(role => {
      const win = role === "workbench" ? this._getWorkbench() : this[role];
      if (!win || win.isDestroyed() || event.sender !== win.webContents) return false;
      try {
        const url = new URL(event.senderFrame.url);
        if (url.origin !== this._apiBase) return false;
        return role === "workbench" ? ["/desktop/", "/desktop/index.html"].includes(url.pathname)
          : url.pathname === "/desktop/desktop-patrol.html" && url.searchParams.get("surface") === role;
      } catch { return false; }
    });
  }

  restore() {
    if (this._disposed) return;
    this._reposition();
    this.robot.showInactive();
    if (this.panel.isVisible()) this.panel.focus();
  }

  openPanel(target = {}) {
    const valid = validateTarget(target);
    this.robot.showInactive();
    this._positionPanel();
    this.panel.show();
    this.panel.focus();
    if (this._ready.has("panel")) this.panel.webContents.send(`${PREFIX}open`, valid);
    else this._panelTarget = valid;
    return true;
  }

  async openWorkbench(target = {}) {
    this._workbenchTarget = validateTarget(target);
    await this._openWorkbench();
    this._flushWorkbench();
    return true;
  }

  async requestQuit() {
    if (this._quitConfirmed) return this._quit();
    if (this._quitPrompt) return this._quitPrompt;
    this._quitPrompt = (async () => {
      if (this.hasUnconfirmedInput()) {
        const { response } = await this._electron.dialog.showMessageBox({
          type: "warning", title: "退出 Focus", message: "还有未发送的草稿或尚未确认的发送。",
          detail: "退出后未发送的预览不会恢复；已受理的工作保留在工作区中。", buttons: ["继续编辑", "退出 Focus"],
          defaultId: 0, cancelId: 0, noLink: true,
        });
        if (response !== 1) { this.openPanel(); return false; }
      }
      this._quitConfirmed = true;
      this._quit();
      return true;
    })();
    try { return await this._quitPrompt; } finally { this._quitPrompt = null; }
  }

  hasUnconfirmedInput() {
    return !this._quitConfirmed && (this._state.dirty || this._state.pending || this._queue.length > 0);
  }

  dispose() {
    if (this._disposed) return;
    this._writePreferences();
    this._disposed = true;
    clearTimeout(this._saveTimer);
    for (const name of this._handlers) this._electron.ipcMain.removeHandler(name);
    if (this._onDisplaysChanged) {
      for (const event of ["display-added", "display-removed", "display-metrics-changed"]) this._electron.screen.removeListener(event, this._onDisplaysChanged);
    }
    this._tray?.destroy();
    for (const win of [this.robot, this.panel]) if (win && !win.isDestroyed()) win.destroy();
    this._queue.length = 0;
  }

  _url(surface) { return `${this._apiBase}/desktop/desktop-patrol.html?surface=${surface}`; }

  _protectWindow(win, surface) {
    win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
    const protect = (event, target) => {
      if (String(target).split("#")[0] !== this._url(surface)) event.preventDefault();
    };
    win.webContents.on("will-navigate", protect);
    win.webContents.on("will-redirect", protect);
  }

  _registerIpc() {
    const handle = (name, roles, callback) => {
      const channel = `${PREFIX}${name}`;
      this._electron.ipcMain.handle(channel, (event, value) => {
        if (!this.accepts(event, roles)) throw new Error("此窗口不能调用桌面 Patrol 操作");
        return callback(value, event);
      });
      this._handlers.push(channel);
    };
    handle("open", ["robot", "panel", "workbench"], value => this.openPanel(value));
    handle("hide-panel", ["robot", "panel"], () => this.panel.hide());
    handle("hide-robot", ["robot", "panel"], () => { this.panel.hide(); this.robot.hide(); });
    handle("workbench", ["robot", "panel"], value => this.openWorkbench(value));
    handle("receive", ["robot"], value => {
      const content = validateContent(value, this._limits);
      if (this._queuedBytes + content.totalBytes > this._limits.general_file_bytes) throw new Error("待预览材料超出总大小限制");
      if (this._queue.reduce((count, row) => count + row.items.length, 0) + content.items.length > this._limits.run_material_count) {
        throw new Error("待预览材料数量超出限制");
      }
      this.openPanel();
      const workspaceId = Object.hasOwn(value, "workspaceId")
        ? boundedString(value.workspaceId, 160, "接收时工作区", true) : this._state.workspaceId;
      const payload = { items: content.items, workspaceId };
      if (this._ready.has("panel")) this.panel.webContents.send(`${PREFIX}content`, payload);
      else { this._queue.push(payload); this._queuedBytes += content.totalBytes; }
      return true;
    });
    handle("move", ["robot"], value => {
      if (!Number.isFinite(value?.x) || !Number.isFinite(value?.y)) throw new Error("机器人位置无效");
      const desired = { x: Math.round(value.x), y: Math.round(value.y), ...ROBOT_SIZE };
      const area = this._electron.screen.getDisplayMatching(desired).workArea;
      this.robot.setBounds(clampBounds(desired, area));
      this._positionPanel();
      this._saveSoon();
      return this.robot.getBounds();
    });
    handle("state", ["panel"], value => {
      const next = {
        workspaceId: boundedString(value?.workspaceId, 160, "工作区", true),
        workspaceName: boundedString(value?.workspaceName, 255, "工作区名称", true),
        state: boundedString(value?.state, 64, "状态"),
        dirty: value?.dirty === true, pending: value?.pending === true,
      };
      this._state = next;
      if (this._ready.has("robot")) this.robot.webContents.send(`${PREFIX}state`, next);
      this._saveSoon();
      return true;
    });
    handle("ready", ["robot", "panel", "workbench"], (_value, event) => {
      const surface = this.accepts(event, ["robot"]) ? "robot" : this.accepts(event, ["panel"]) ? "panel" : "workbench";
      this._ready.add(surface);
      if (surface === "robot") this.robot.webContents.send(`${PREFIX}state`, this._state);
      if (surface === "panel") {
        this.panel.webContents.send(`${PREFIX}open`, this._panelTarget || { workspaceId: this._state.workspaceId });
        this._panelTarget = null;
        for (const payload of this._queue) this.panel.webContents.send(`${PREFIX}content`, payload);
        this._queue = [];
        this._queuedBytes = 0;
      }
      if (surface === "workbench") {
        if (this._workbenchContents !== event.sender) {
          this._workbenchContents = event.sender;
          const reset = () => this._ready.delete("workbench");
          event.sender.on("did-start-loading", reset);
          event.sender.once("destroyed", reset);
        }
        this._flushWorkbench();
      }
      return { state: this._state, limits: this._limits, bounds: this.robot.getBounds() };
    });
  }

  _flushWorkbench() {
    const win = this._getWorkbench();
    if (this._workbenchTarget && this._ready.has("workbench") && win && !win.isDestroyed()) {
      win.webContents.send(`${PREFIX}open-workbench`, this._workbenchTarget);
      this._workbenchTarget = null;
    }
  }

  _positionPanel() {
    if (!this.robot || this.robot.isDestroyed() || !this.panel || this.panel.isDestroyed()) return;
    const robot = this.robot.getBounds();
    this.panel.setBounds(panelBounds(robot, this._electron.screen.getDisplayMatching(robot).workArea));
  }

  _reposition() {
    if (!this.robot || this.robot.isDestroyed()) return;
    const current = this.robot.getBounds();
    this.robot.setBounds(clampBounds(current, this._electron.screen.getDisplayMatching(current).workArea));
    this._positionPanel();
    this._saveSoon();
  }

  _readPreferences() {
    try {
      const saved = JSON.parse(fs.readFileSync(this._preferencesPath, "utf8"));
      if (Number.isFinite(saved.x) && Number.isFinite(saved.y)) this._position = { x: saved.x, y: saved.y };
      this._state.workspaceId = boundedString(saved.workspaceId, 160, "工作区", true);
      this._state.workspaceName = boundedString(saved.workspaceName, 255, "工作区名称", true);
    } catch {}
  }

  _saveSoon() {
    clearTimeout(this._saveTimer);
    this._saveTimer = setTimeout(() => this._writePreferences(), 200);
  }

  _writePreferences() {
    if (!this.robot || this.robot.isDestroyed()) return;
    const { x, y } = this.robot.getBounds();
    const { workspaceId, workspaceName } = this._state;
    try {
      fs.mkdirSync(path.dirname(this._preferencesPath), { recursive: true });
      fs.writeFileSync(`${this._preferencesPath}.tmp`, JSON.stringify({ x, y, workspaceId, workspaceName }), "utf8");
      fs.renameSync(`${this._preferencesPath}.tmp`, this._preferencesPath);
    } catch (error) { console.error("保存桌面机器人偏好失败:", error.message); }
  }
}

module.exports = { DesktopPatrolHost, clampBounds, panelBounds, validateContent };
