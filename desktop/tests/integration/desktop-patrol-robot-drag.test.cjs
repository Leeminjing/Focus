/* Production robot/preload/IPC drag regression in isolated Electron windows.
 * Chromium input covers page zoom and native repositioning, not OS hit testing.
 */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

if (!process.versions.electron) {
  const { test } = require("node:test");
  const { spawn } = require("node:child_process");
  for (const scale of [1, 1.25, 1.5]) test(`robot drag uses native coordinates at ${scale}x OS scale`, { timeout: 20000 }, async () => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "focus-robot-drag-"));
    const env = { ...process.env, FOCUS_ROBOT_DRAG_USER_DATA: directory };
    delete env.ELECTRON_RUN_AS_NODE;
    const child = spawn(require("electron"), [`--force-device-scale-factor=${scale}`, __filename], {
      windowsHide: true, env, stdio: ["ignore", "pipe", "pipe"],
    });
    let output = "";
    child.stdout.on("data", value => { output += value; });
    child.stderr.on("data", value => { output += value; });
    const timeout = setTimeout(() => child.kill(), 15000);
    try {
      const code = await new Promise((resolve, reject) => { child.once("exit", resolve); child.once("error", reject); });
      assert.equal(code, 0, output);
      assert.match(output, /ROBOT_DRAG_PASS zoom=-2,0,2/);
      console.log(output.trim());
    } finally {
      clearTimeout(timeout);
      assert.ok(path.resolve(directory).startsWith(path.resolve(os.tmpdir()) + path.sep));
      fs.rmSync(directory, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
    }
  });
} else {
  const electron = require("electron");
  const { app, BrowserWindow, ipcMain, screen } = electron;
  const http = require("node:http");
  const { setTimeout: delay } = require("node:timers/promises");
  const { DesktopPatrolHost } = require("../../src/main/desktop-patrol-host.cjs");
  const { registerDesktopPreferences } = require("../../src/main/desktop-preferences.cjs");
  const directory = process.env.FOCUS_ROBOT_DRAG_USER_DATA;
  app.setPath("userData", directory);
  app.on("window-all-closed", () => {});
  // This test never activates a window or sends operating-system mouse input.
  const showInactive = BrowserWindow.prototype.showInactive;
  BrowserWindow.prototype.show = function () { return showInactive.call(this); };
  BrowserWindow.prototype.focus = function () {};
  const renderer = path.resolve(__dirname, "../../src/renderer");
  const limits = { general_file_bytes: 1048576, original_image_bytes: 262144, run_material_count: 8 };
  let host, server;
  const moves = [], errors = [];
  const register = ipcMain.handle.bind(ipcMain);
  ipcMain.handle = (channel, handler) => register(channel, (event, value) => {
    if (channel === "focus:patrol-move") moves.push(value);
    return handler(event, value);
  });
  app.on("web-contents-created", (_event, contents) => {
    contents.on("preload-error", (_event, _path, error) => errors.push(error.message));
    contents.on("render-process-gone", (_event, details) => errors.push(details.reason));
  });
  const run = (win, source) => win.webContents.executeJavaScript(source);
  async function until(predicate, message) {
    for (let attempt = 0; attempt < 100; attempt++) {
      if (await predicate()) return;
      await delay(20);
    }
    throw new Error(message);
  }
  async function execute() {
    server = http.createServer((request, response) => {
      const pathname = new URL(request.url, "http://fixture.local").pathname;
      const file = path.resolve(renderer, decodeURIComponent(pathname.replace(/^\/desktop\//, "")));
      if (!file.startsWith(renderer + path.sep) || !fs.existsSync(file)) {
        response.writeHead(404); response.end(); return;
      }
      response.writeHead(200, { "Content-Type": { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".png": "image/png", ".svg": "image/svg+xml" }[path.extname(file)] || "application/octet-stream" });
      response.end(fs.readFileSync(file));
    });
    await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
    const apiBase = `http://127.0.0.1:${server.address().port}`;
    process.env.FOCUS_DESKTOP_API = apiBase;
    process.env.FOCUS_DESKTOP_SESSION = "isolated-robot-drag";
    ipcMain.handle("focus:gateway-status", () => ({ status: "ready", revision: 1 }));
    registerDesktopPreferences(ipcMain, { directory: () => directory, authorize: event => assert.ok(host.accepts(event)) });
    host = new DesktopPatrolHost({ electron, apiBase, desktopDir: path.resolve(__dirname, "../.."),
      icon: path.join(renderer, "assets/focus-icon.ico"), limits, getWorkbench: () => null,
      openWorkbench: async () => { throw new Error("Unexpected workbench"); }, quit: () => {} });
    await host.start();
    await until(() => host._ready.has("robot") && host._ready.has("panel"), "Production preload did not become ready");
    const win = host.robot;
    for (const zoom of [-2, 0, 2]) {
      win.webContents.setZoomLevel(zoom);
      // Native moves, including negative desktop coordinates, must not turn the
      // renderer's window origin into page-scaled CSS coordinates.
      for (const position of [{ x: -240, y: -120 }, ...screen.getAllDisplays().map(display => ({ x: display.workArea.x + 24, y: display.workArea.y + 24 }))]) {
        win.setBounds({ ...position, width: 96, height: 120 });
        await until(async () => {
          const origin = await run(win, "({x:screenX,y:screenY})"), bounds = win.getBounds();
          return Math.abs(origin.x - bounds.x) <= 1 && Math.abs(origin.y - bounds.y) <= 1;
        }, `Window screen origin differs from native bounds at zoom ${zoom}`);
      }
      const area = screen.getPrimaryDisplay().workArea;
      win.setBounds({ x: area.x + 100, y: area.y + 100, width: 96, height: 120 });
      await delay(60);
      const before = win.getBounds(), moveCount = moves.length;
      win.webContents.sendInputEvent({ type: "mouseDown", x: 30, y: 30, globalX: before.x + 30, globalY: before.y + 30, button: "left", clickCount: 1 });
      await delay(20);
      win.webContents.sendInputEvent({ type: "mouseMove", x: 60, y: 50, globalX: before.x + 60, globalY: before.y + 50, button: "left" });
      await until(() => moves.length > moveCount, `Drag did not reach production move IPC at zoom ${zoom}`);
      win.webContents.sendInputEvent({ type: "mouseUp", x: 60, y: 50, button: "left", clickCount: 1 });
      await delay(30);
      const after = win.getBounds();
      assert.ok(Math.abs(after.x - before.x - 30) <= 1 && Math.abs(after.y - before.y - 20) <= 1,
        `zoom ${zoom}: dragging +30,+20 changed ${JSON.stringify(before)} to ${JSON.stringify(after)}; IPC=${JSON.stringify(moves.at(-1))}`);
    }
    assert.deepEqual(errors, []);
    console.log("ROBOT_DRAG_PASS zoom=-2,0,2");
  }
  function cleanup(code) {
    host?.dispose(); server?.closeAllConnections(); server?.close(); app.exit(code);
  }
  app.whenReady().then(execute).then(() => cleanup(0)).catch(error => { console.error(error.stack); cleanup(1); });
}
