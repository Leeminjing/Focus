/* Production robot/preload/IPC interaction regression in isolated Electron windows.
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
  for (const scale of [1, 1.25, 1.5]) test(`robot toggles its panel and drags in native coordinates at ${scale}x OS scale`, { timeout: 20000 }, async () => {
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
      assert.match(output, /ROBOT_TOGGLE_PASS/);
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
  const moves = [], errors = [], toggles = [], submissions = [], nativeOptions = new Map();
  const register = ipcMain.handle.bind(ipcMain);
  ipcMain.handle = (channel, handler) => register(channel, (event, value) => {
    if (channel === "focus:patrol-move") moves.push(value);
    if (channel === "focus:patrol-toggle") toggles.push(value);
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
      if (request.method === "POST" && pathname.endsWith("/inputs")) submissions.push(pathname);
      if (pathname.endsWith("/materials/limits")) {
        response.writeHead(200, { "Content-Type": "application/json" }); response.end(JSON.stringify(limits)); return;
      }
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
    host = new DesktopPatrolHost({ electron: { ...electron, BrowserWindow: function (options) {
      const window = new BrowserWindow(options); nativeOptions.set(window, options); return window;
    } }, apiBase, desktopDir: path.resolve(__dirname, "../.."),
      icon: path.join(renderer, "assets/focus-icon.ico"), limits, getWorkbench: () => null,
      openWorkbench: async () => { throw new Error("Unexpected workbench"); }, quit: () => {} });
    await host.start();
    await until(() => host._ready.has("robot") && host._ready.has("panel"), "Production preload did not become ready");
    const win = host.robot;
    assert.equal(nativeOptions.get(win).transparent, true);
    assert.equal(nativeOptions.get(host.panel).transparent, process.platform !== "win32", "Windows panel must use an opaque native surface");
    assert.equal(await run(host.panel, "window.focusDesktop.patrol.opaquePanel"), process.platform === "win32");
    const clickRobot = () => run(win, "document.querySelector('#robot-open').click()");
    await run(host.panel, "Promise.all([document.fonts.ready, ...document.querySelector('#panel').getAnimations().map(animation => animation.finished.catch(() => {}))])");
    // Expose the native-show / renderer-open race with one scheduling delay and
    // observe computed styles between class changes, as a layout reader can.
    // Capture only this isolated window; no desktop screen capture.
    await run(host.panel, "window.openingObserver = new MutationObserver(() => { getComputedStyle(document.querySelector('#panel')).opacity; }); window.openingObserver.observe(document.querySelector('#panel'), {attributes:true,attributeFilter:['class']})");
    const send = host.panel.webContents.send;
    let delayedOpen = false;
    host.panel.webContents.send = function (channel, ...args) {
      if (channel === "focus:patrol-open" && !delayedOpen) {
        delayedOpen = true;
        setTimeout(() => send.call(this, channel, ...args), 80);
        return;
      }
      return send.call(this, channel, ...args);
    };
    let openingFrames;
    try {
      await clickRobot();
      await until(() => host.panel.isVisible(), "Opening frame regression never showed the native panel");
      openingFrames = [];
      const start = Date.now();
      for (const ms of [0, 16, 48, 96, 144, 240, 480]) {
        await delay(Math.max(0, start + ms - Date.now()));
        const image = await host.panel.webContents.capturePage(), pixels = image.toBitmap();
        let alpha = 0;
        for (let index = 3; index < pixels.length; index += 4) alpha += pixels[index];
        const opacity = await run(host.panel, "Number(getComputedStyle(document.querySelector('#panel')).opacity)");
        openingFrames.push({ ms, elapsed: Date.now() - start, meanAlpha: alpha / (pixels.length / 4), pixels: pixels.length / 4, opacity });
      }
    } finally {
      host.panel.webContents.send = send;
      await run(host.panel, "window.openingObserver.disconnect(); delete window.openingObserver");
    }
    assert.equal(delayedOpen, true, "The timing-pressure test must reach the production open IPC");
    assert.equal(openingFrames.length, 7);
    assert.ok(openingFrames.every(frame => frame.pixels > 0 && Number.isFinite(frame.meanAlpha)), "Every opening sample must contain actual pixels");
    assert.ok(openingFrames[0].meanAlpha > 100 && openingFrames.at(-1).meanAlpha > 100, `Opening samples must include the card: ${JSON.stringify(openingFrames)}`);
    assert.ok(openingFrames.every(frame => frame.meanAlpha >= openingFrames[0].meanAlpha * 0.8),
      `Opening regressed from a complete card to transparency: ${JSON.stringify(openingFrames)}`);
    assert.ok(openingFrames.every(frame => frame.opacity >= 0.8), "Opaque native pixels must not mask a renderer opacity restart");
    await clickRobot();
    await until(() => !host.panel.isVisible(), "Opening frame regression did not restore the hidden panel");
    await clickRobot();
    await until(() => host.panel.isVisible(), "Robot click did not open the panel");
    await run(host.panel, "document.querySelector('#instruction').value='Preserve this draft'; document.querySelector('#instruction').dispatchEvent(new Event('input',{bubbles:true}))");
    await clickRobot();
    await until(() => !host.panel.isVisible(), "Second robot click did not collapse the panel");
    await host.flushRecovery();
    assert.equal(host._recovery.read().state.workspaces.find(([id]) => id === null)[1].draft.content, "Preserve this draft");
    await clickRobot();
    await until(() => host.panel.isVisible(), "Third robot click did not reopen the panel");
    assert.equal(await run(host.panel, "document.querySelector('#instruction').value"), "Preserve this draft");
    await run(host.panel, "window.closingSamples=[]; window.closingObserver=new MutationObserver(() => {const node=document.querySelector('#panel');if(node.classList.contains('is-closing')){const style=getComputedStyle(node);window.closingSamples.push({opacity:style.opacity,animation:style.animationName});}}); window.closingObserver.observe(document.querySelector('#panel'),{attributes:true,attributeFilter:['class']})");
    await run(host.panel, "document.querySelector('#hide').click()");
    await until(() => !host.panel.isVisible(), "Panel's own hide button did not collapse it");
    const closingSamples = await run(host.panel, "window.closingObserver.disconnect(); delete window.closingObserver; window.closingSamples");
    if (process.platform === "win32") assert.ok(closingSamples.length > 0 && closingSamples.every(sample => sample.opacity === "1" && sample.animation === "none"),
      `Opaque panel must hide without fading into its native background: ${JSON.stringify(closingSamples)}`);
    await clickRobot();
    await until(() => host.panel.isVisible(), "Robot click did not follow the panel's actual hidden state");
    await run(host.panel, "document.querySelector('#hide').click()");
    if (process.platform === "win32") await until(() => !host.panel.isVisible(), "Opaque panel should hide directly");
    else await until(async () => await run(host.panel, "document.querySelector('#panel').classList.contains('is-closing')"), "Panel close animation did not start");
    await run(win, "window.focusDesktop.patrol.openPanel()");
    await delay(420);
    assert.equal(host.panel.isVisible(), true, "A previous close animation must not hide a rapidly reopened panel");
    assert.equal(await run(host.panel, "document.querySelector('#panel').inert"), false, "Rapidly reopened panel must accept input");
    const toggleCount = toggles.length;
    await run(win, "document.querySelector('#robot-open').click(); document.querySelector('#robot-open').click()");
    await until(() => toggles.length === toggleCount + 2, "Rapid robot clicks did not reach toggle IPC");
    assert.equal(host.panel.isVisible(), true, "Two rapid clicks must restore the starting visible state");
    await run(win, "Promise.all([window.focusDesktop.patrol.openPanel(), window.focusDesktop.patrol.openPanel()])");
    assert.equal(host.panel.isVisible(), true, "Explicit open remains idempotent, not a toggle");
    await run(win, "window.focusDesktop.patrol.receive({items:[{kind:'text',text:'Reference only'}]})");
    await until(async () => await run(host.panel, "document.querySelectorAll('.patrol-material-card').length === 1"), "Received reference was not previewed");
    assert.equal(host.panel.isVisible(), true, "Receiving material must leave an open panel visible");
    assert.deepEqual(submissions, [], "Visibility changes and references must not submit the draft");
    console.log("ROBOT_TOGGLE_PASS");
    for (const zoom of [-2, 0, 2]) {
      win.webContents.setZoomLevel(zoom);
      if (process.platform === "win32") await until(async () => {
        const layout = await run(host.panel, "(() => {const shell=document.querySelector('.panel-shell').getBoundingClientRect();return {x:shell.x,y:shell.y,width:shell.width,height:shell.height,innerWidth,innerHeight};})()"), native = host.panel.getBounds();
        const factor = host.panel.webContents.getZoomFactor();
        return layout.x === 0 && layout.y === 0 && Math.abs(layout.width-layout.innerWidth) <= 1 && Math.abs(layout.height-layout.innerHeight) <= 1
          && Math.abs(native.width-layout.width*factor) <= 2 && Math.abs(native.height-layout.height*factor) <= 2;
      }, `Opaque panel must fit its card without native white gutters at zoom ${zoom}`);
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
      const before = win.getBounds(), moveCount = moves.length, toggleCount = toggles.length;
      const panelVisible = host.panel.isVisible();
      win.webContents.sendInputEvent({ type: "mouseDown", x: 30, y: 30, globalX: before.x + 30, globalY: before.y + 30, button: "left", clickCount: 1 });
      await delay(20);
      win.webContents.sendInputEvent({ type: "mouseMove", x: 60, y: 50, globalX: before.x + 60, globalY: before.y + 50, button: "left" });
      await until(() => moves.length > moveCount, `Drag did not reach production move IPC at zoom ${zoom}`);
      win.webContents.sendInputEvent({ type: "mouseUp", x: 60, y: 50, button: "left", clickCount: 1 });
      await delay(30);
      const after = win.getBounds();
      assert.ok(Math.abs(after.x - before.x - 30) <= 1 && Math.abs(after.y - before.y - 20) <= 1,
        `zoom ${zoom}: dragging +30,+20 changed ${JSON.stringify(before)} to ${JSON.stringify(after)}; IPC=${JSON.stringify(moves.at(-1))}`);
      assert.equal(toggles.length, toggleCount, `Dragging at zoom ${zoom} must not toggle the panel`);
      assert.equal(host.panel.isVisible(), panelVisible);
    }
    assert.deepEqual(errors, []);
    console.log("ROBOT_DRAG_PASS zoom=-2,0,2");
  }
  function cleanup(code) {
    host?.dispose(); server?.closeAllConnections(); server?.close(); app.exit(code);
  }
  app.whenReady().then(execute).then(() => cleanup(0)).catch(error => { console.error(error.stack); cleanup(1); });
}
