/*
 * 本文件对外提供桌面 Patrol 宿主的安全、位置和窗口生命周期回归。
 * 输入为受控 Electron 窗口/IPC、临时偏好目录和材料上限；输出为越权/越界拒绝、草稿退出保护与显示器恢复断言。
 * 工作流为调用生产宿主，经真实注册的 handler 转交有界字节，检查窗口保持和显式退出；不替代原生拖放验收。
 * 示例：node --test desktop/desktop-patrol-host.test.cjs。
 */
"use strict";

const assert = require("node:assert/strict");
const { test } = require("node:test");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { DesktopPatrolHost, clampBounds, panelBounds, validateContent } = require("./desktop-patrol-host.cjs");

const limits = { general_file_bytes: 32, run_material_count: 3 };
const originalArea = { x: 0, y: 0, width: 1920, height: 1040 };

function fixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "focus-patrol-host-"));
  const handlers = new Map();
  let workbench = null, quits = 0, promptResponse = 0, prompts = 0;
  class Window extends EventEmitter {
    constructor(options = {}) {
      super();
      this.options = options;
      this.bounds = { x: options.x || 0, y: options.y || 0, width: options.width || 900, height: options.height || 700 };
      this.messages = [];
      this.focuses = 0;
      this.webContents = new EventEmitter();
      this.webContents.mainFrame = { url: "" };
      this.webContents.setWindowOpenHandler = callback => { this.openHandler = callback; };
      this.webContents.send = (channel, value) => this.messages.push({ channel, value });
    }
    async loadURL(url) { this.webContents.emit("did-start-loading"); this.webContents.mainFrame.url = url; }
    getBounds() { return { ...this.bounds }; }
    setBounds(value) { this.bounds = { ...value }; }
    showInactive() { this.visible = true; }
    show() { this.visible = true; }
    hide() { this.visible = false; }
    focus() { this.focuses++; }
    isVisible() { return this.visible === true; }
    isDestroyed() { return this.destroyed === true; }
    destroy() { this.destroyed = true; }
    close() { const event = { preventDefault() { this.prevented = true; } }; this.emit("close", event); if (!event.prevented) this.destroy(); }
  }
  const screen = Object.assign(new EventEmitter(), {
    area: { ...originalArea },
    getPrimaryDisplay() { return { workArea: this.area }; },
    getDisplayMatching() { return { workArea: this.area }; },
  });
  const electron = {
    BrowserWindow: Window, screen, app: { getPath: () => directory },
    ipcMain: Object.assign(new EventEmitter(), { handle: (name, callback) => handlers.set(name, callback), removeHandler: name => handlers.delete(name) }),
    Menu: { buildFromTemplate: value => value },
    Tray: class extends EventEmitter { setToolTip() {} setContextMenu(value) { this.menu = value; } destroy() {} },
    dialog: { showMessageBox: async () => { prompts++; return { response: promptResponse }; } },
  };
  const host = new DesktopPatrolHost({
    electron, apiBase: "http://127.0.0.1:8123", desktopDir: __dirname, icon: "icon.ico", limits,
    openWorkbench: async () => {
      if (!workbench) { workbench = new Window(); await workbench.loadURL("http://127.0.0.1:8123/desktop/"); }
      workbench.show();
    },
    getWorkbench: () => workbench, quit: () => { quits++; },
  });
  const event = win => ({ sender: win.webContents, senderFrame: win.webContents.mainFrame });
  const invoke = (name, win, value) => handlers.get(`focus:patrol-${name}`)(event(win), value);
  t.after(() => {
    host.dispose();
    assert.ok(path.resolve(directory).startsWith(path.resolve(os.tmpdir()) + path.sep));
    fs.rmSync(directory, { recursive: true, force: true });
  });
  const recover = (name, senderEvent, value) => {
    electron.ipcMain.emit(`focus:patrol-recovery-${name}`, senderEvent, value);
    return senderEvent.returnValue;
  };
  return { host, screen, handlers, invoke, recover, event, Window, directory,
    get workbench() { return workbench; }, get quits() { return quits; }, get prompts() { return prompts; },
    confirmQuit() { promptResponse = 1; } };
}

test("geometry keeps both surfaces reachable across negative coordinates and small displays", () => {
  const area = { x: -1280, y: -300, width: 1280, height: 720 };
  assert.deepEqual(clampBounds({ x: 5000, y: -1000, width: 144, height: 164 }, area), { x: -144, y: -300, width: 144, height: 164 });
  const leftRobot = { x: -1280, y: 100, width: 144, height: 164 };
  const panel = panelBounds(leftRobot, area);
  assert.equal(panel.x, -1128);
  assert.ok(panel.y >= area.y && panel.y + panel.height <= area.y + area.height);
  assert.deepEqual(clampBounds({ x: 5, y: 5, width: 440, height: 620 }, { x: 0, y: 0, width: 400, height: 300 }), { x: 0, y: 0, width: 400, height: 300 });
});

test("content accepts pathless clipboard bytes and bounds the entire UTF-8 batch", () => {
  assert.equal(validateContent({ items: [{ kind: "file", name: "clipboard.png", type: "image/png", bytes: new Uint8Array([1, 2]) }] }, limits).totalBytes, 2);
  assert.equal(validateContent({ items: [{ kind: "text", text: "截图" }] }, limits).totalBytes, 6);
  for (const invalid of [
    { items: [{ kind: "directory", name: "folder" }] },
    { items: [{ kind: "file", name: "C:\\secret.png", bytes: new Uint8Array([1]) }] },
    { items: [{ kind: "file", name: "clipboard.png", path: "C:\\secret.png" }] },
    { items: [{ kind: "file", name: "clipboard.png", bytes: [1, 2] }] },
    { items: [{ kind: "file", name: "clipboard.png", bytes: new Uint8Array(new ArrayBuffer(64), 0, 1) }] },
    { items: [{ kind: "text", text: "图".repeat(12) }] },
    { items: [{ kind: "file", name: "a", bytes: new Uint8Array(30) }, { kind: "text", text: "abc" }] },
  ]) assert.throws(() => validateContent(invalid, limits));
});

test("compact panel expands within display bounds and only accepts resize from its owner", async t => {
  const f = fixture(t); await f.host.start();
  assert.equal(f.host.panel.getBounds().width, 560);
  assert.equal(f.host.panel.getBounds().height, 202);
  assert.throws(() => f.invoke("resize-panel", f.host.robot, 500), /不能调用/);
  for (const value of [null, "400", NaN, Infinity]) assert.throws(() => f.invoke("resize-panel", f.host.panel, value), /高度无效/);
  f.invoke("resize-panel", f.host.panel, 10000);
  assert.equal(f.host.panel.getBounds().height, 620);
  f.host.openPanel();
  assert.equal(f.host.panel.getBounds().height, 620);
  f.screen.area = { x: 0, y: 0, width: 400, height: 300 };
  f.screen.emit("display-metrics-changed");
  assert.equal(f.host.panel.getBounds().height, 300);
  assert.equal(f.host.panel.getBounds().width, 400);
  f.invoke("resize-panel", f.host.panel, 0);
  assert.equal(f.host.panel.getBounds().height, 202);
});

test("IPC authenticates owner window, main frame, origin and surface before transfer", async t => {
  const f = fixture(t); await f.host.start();
  const handler = f.handlers.get("focus:patrol-receive");
  const content = { items: [{ kind: "text", text: "reference" }] };
  assert.throws(() => handler(f.event(f.host.panel), content), /不能调用/);
  assert.throws(() => handler({ sender: f.host.robot.webContents, senderFrame: { url: f.host.robot.webContents.mainFrame.url } }, content), /不能调用/);
  const impostor = new f.Window(); await impostor.loadURL(f.host.robot.webContents.mainFrame.url);
  assert.throws(() => handler(f.event(impostor), content), /不能调用/);
  f.host.robot.webContents.mainFrame.url = "https://example.com/desktop/desktop-patrol.html?surface=robot";
  assert.throws(() => handler(f.event(f.host.robot), content), /不能调用/);
  await f.host.robot.loadURL("http://127.0.0.1:8123/desktop/desktop-patrol.html?surface=robot");
  f.invoke("receive", f.host.robot, content);
  assert.equal(f.host.panel.messages.length, 0);
  const ready = f.invoke("ready", f.host.panel);
  assert.deepEqual(ready.limits, limits);
  assert.deepEqual(f.host.panel.messages.find(row => row.channel.endsWith("content")).value.items, content.items);
  assert.deepEqual(f.host.robot.openHandler({ url: "https://example.com" }), { action: "deny" });
  for (const target of ["https://example.com", "http://127.0.0.1:8123/desktop/", "file:///C:/secret"]) {
    let prevented = false;
    f.host.robot.webContents.emit("will-navigate", { preventDefault: () => { prevented = true; } }, target);
    assert.equal(prevented, true);
  }
});

test("recovery IPC belongs only to the panel main frame and acknowledges durable writes", async t => {
  const f = fixture(t); await f.host.start(); await f.host.openWorkbench();
  for (const win of [f.host.robot, f.workbench]) assert.match(f.recover("read", f.event(win)).error, /不能访问/);
  assert.match(f.recover("read", { sender: f.host.panel.webContents, senderFrame: { url: f.host.panel.webContents.mainFrame.url } }).error, /不能访问/);
  assert.deepEqual(f.recover("read", f.event(f.host.panel)).value, { state: null, sources: [] });
  assert.equal(f.recover("put", f.event(f.host.panel), { id: "local", bytes: new Uint8Array([1, 2]) }).value, true);
  const state = { version: 1, workspaces: [], source_ids: ["local"] };
  assert.equal(f.recover("save", f.event(f.host.panel), state).value, true);
  assert.deepEqual(f.recover("read", f.event(f.host.panel)).value, { state, sources: [["local", new Uint8Array([1, 2])]] });
  assert.match(f.recover("put", f.event(f.host.panel), { id: "too-big", bytes: new Uint8Array(33) }).error, /总量超限/);
  f.host.panel.webContents.mainFrame.url = "https://example.com/desktop/desktop-patrol.html?surface=panel";
  assert.match(f.recover("read", f.event(f.host.panel)).error, /不能访问/);
});

test("queued content is bounded, panel hide preserves owner and background changes never focus", async t => {
  const f = fixture(t); await f.host.start();
  f.invoke("receive", f.host.robot, { items: [{ kind: "text", text: "x".repeat(20) }] });
  assert.throws(() => f.invoke("receive", f.host.robot, { items: [{ kind: "text", text: "y".repeat(20) }] }), /总大小/);
  f.invoke("ready", f.host.panel); f.invoke("ready", f.host.robot);
  const owner = f.host.panel;
  f.host.panel.close();
  assert.equal(owner.isDestroyed(), false);
  assert.equal(owner.isVisible(), false);
  const focused = owner.focuses;
  f.invoke("state", owner, { workspaceId: "ws-a", workspaceName: "A", state: "working", dirty: true, pending: false });
  f.screen.area = { x: 0, y: 0, width: 800, height: 600 };
  f.screen.emit("display-removed");
  assert.equal(owner.focuses, focused);
  assert.equal(f.host.robot.focuses, 0);
  assert.ok(f.host.robot.getBounds().x + 144 <= 800);
  assert.ok(owner.getBounds().y + owner.getBounds().height <= 600);
  f.host.restore();
  assert.equal(owner.isVisible(), false);
  f.host.openPanel();
  assert.equal(f.host.panel, owner);
  assert.equal(f.host.hasUnconfirmedInput(), true);
});

test("a slow file read retains its captured workspace even after binding changes", async t => {
  const f = fixture(t); await f.host.start();
  f.invoke("ready", f.host.panel);
  f.invoke("state", f.host.panel, { workspaceId: "ws-b", workspaceName: "B", state: "idle", dirty: false, pending: false });
  f.invoke("receive", f.host.robot, { workspaceId: "ws-a", items: [{ kind: "file", name: "a.png", bytes: new Uint8Array([1]) }] });
  f.invoke("receive", f.host.robot, { workspaceId: "", items: [{ kind: "text", text: "unbound" }] });
  const delivered = f.host.panel.messages.filter(row => row.channel.endsWith("content"));
  assert.deepEqual(delivered.map(row => row.value.workspaceId), ["ws-a", ""]);
  assert.throws(() => f.invoke("receive", f.host.robot, { workspaceId: { arbitrary: true }, items: [{ kind: "text", text: "invalid" }] }), /工作区/);
});

test("quit requires an explicit decision for drafts and persists only identity/position", async t => {
  const f = fixture(t); await f.host.start();
  f.invoke("state", f.host.panel, { workspaceId: "ws-a", workspaceName: "A", state: "sending", dirty: false, pending: true });
  assert.equal(await f.host.requestQuit(), false);
  assert.equal(f.quits, 0);
  f.confirmQuit();
  assert.equal(await f.host.requestQuit(), true);
  assert.equal(f.quits, 1);
  assert.equal(f.prompts, 2);
  assert.equal(f.host.hasUnconfirmedInput(), false);
  f.host.dispose();
  const saved = JSON.parse(fs.readFileSync(path.join(f.directory, "desktop-patrol.json"), "utf8"));
  assert.deepEqual(Object.keys(saved).sort(), ["workspaceId", "workspaceName", "x", "y"]);
  assert.equal(saved.workspaceId, "ws-a");
  assert.equal(f.handlers.size, 0);
});

test("workbench target waits for its renderer and opening never creates another panel", async t => {
  const f = fixture(t); await f.host.start();
  const owner = f.host.panel;
  await f.host.openWorkbench({ workspaceId: "ws-a", taskId: "task-a", runId: "run-a", revisionId: "revision-a" });
  assert.equal(f.workbench.messages.length, 0);
  f.invoke("ready", f.workbench);
  assert.equal(f.workbench.messages[0].channel, "focus:patrol-open-workbench");
  assert.deepEqual(f.workbench.messages[0].value, { workspaceId: "ws-a", taskId: "task-a", runId: "run-a", revisionId: "revision-a" });
  await f.workbench.loadURL("http://127.0.0.1:8123/desktop/");
  await f.host.openWorkbench({ workspaceId: "ws-b" });
  assert.equal(f.workbench.messages.length, 1, "刷新中的工作台尚不能消费定位请求");
  f.invoke("ready", f.workbench);
  assert.deepEqual(f.workbench.messages[1].value, { workspaceId: "ws-b" });
  f.invoke("open", f.workbench, { workspaceId: "ws-a", requestId: "request-a", requestRevision: 4 });
  f.invoke("ready", owner);
  assert.deepEqual(owner.messages[0].value, { workspaceId: "ws-a", requestId: "request-a", requestRevision: 4 });
  assert.equal(f.host.panel, owner);
});
