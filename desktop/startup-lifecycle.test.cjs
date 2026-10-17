/*
 * 本文件对外提供真实 Electron 主进程的启动失败、取消、兼容工作台与机器人生命周期回归。
 * 输入为隔离 userData、受控 Python/Docker 和 HTTP 健康响应；输出为预检控制台隐藏、窗口存活、退出码与单次后端收口断言。
 * 工作流为逐场景启动真实 main，并验证机器人双窗口、受限 IPC、按需工作台、第二实例与退出提醒；不替代 Explorer 拖放验收。
 * 示例：node --test desktop/startup-lifecycle.test.cjs。
 */
const assert = require("node:assert/strict");
const { setTimeout: delay } = require("node:timers/promises");

if (process.versions.electron) {
  const { app, BrowserWindow } = require("electron");
  const cp = require("node:child_process");
  const nativeSpawn = cp.spawn;
  const { EventEmitter } = require("node:events");
  const { PassThrough } = require("node:stream");
  const http = require("node:http");
  const scenario = process.env.FOCUS_STARTUP_TEST;
  let server, kills = 0, quitting = false, healthRequests = 0, backendStarts = 0, secondInstances = 0;
  const report = value => console.log(`STARTUP_TEST ${JSON.stringify(value)}`);
  const fail = error => { console.error(error); server?.close(); app.exit(90); };

  cp.execFile = (command, args, options, callback) => {
    assert.equal(options.windowsHide, true, "启动依赖与迁移子进程必须隐藏控制台");
    assert.ok(options.timeout > 0, "启动命令必须有超时");
    setImmediate(() => {
      if (scenario === "preflight" && command === "docker") return callback(Object.assign(new Error("fixture"), { code: "ENOENT" }));
      callback(null, args.some(arg => arg.endsWith("database-state.py")) ? '{"status":"current"}' : command === "git" || command === "docker" ? "fixture" : process.execPath);
    });
  };
  cp.spawn = (_command, args) => {
    backendStarts++;
    const backend = Object.assign(new EventEmitter(), {
      stdout: new PassThrough(), stderr: new PassThrough(), killed: false,
      kill() { this.killed = true; kills++; server?.close(); setImmediate(() => this.emit("exit", null, "SIGTERM")); },
    });
    const readyAt = Date.now() + 750;
    server = http.createServer((request, response) => {
      if (request.url === "/health") healthRequests++;
      if (["healthy", "companion"].includes(scenario) && Date.now() >= readyAt) {
        if (request.url === "/desktop/api/materials/limits") {
          assert.equal(request.headers["x-focus-session"], process.env.FOCUS_DESKTOP_SESSION);
          response.setHeader("Content-Type", "application/json");
          response.end(JSON.stringify({ general_file_bytes: 1024, run_material_count: 4 }));
        } else { response.setHeader("Content-Type", "text/html"); response.end("<!doctype html><title>Host fixture</title><body>host fixture</body>"); }
      }
      else if (scenario !== "hung-health" && scenario !== "cancel") { response.writeHead(503); response.end("starting"); }
    });
    server.listen(Number(args[args.indexOf("--port") + 1]), "127.0.0.1");
    if (scenario === "backend-exit") setTimeout(() => { server.close(); backend.emit("exit", 7, null); }, 100);
    if (scenario === "spawn-error") setTimeout(() => backend.emit("error", new Error("fixture: Python spawn failed")), 100);
    if (scenario === "cancel") setTimeout(() => app.quit(), 250);
    return backend;
  };
  app.on("second-instance", () => { secondInstances++; });
  app.on("before-quit", () => {
    if (scenario !== "companion") { quitting = true; server?.close(); }
    report({ quitting: true, kills });
  });
  app.on("will-quit", () => { server?.close(); report({ backendKills: kills }); });
  app.on("browser-window-created", (_event, win) => {
    win.webContents.on("did-finish-load", async () => {
      try {
        const url = win.webContents.getURL();
        if (url.startsWith("data:text/html")) {
          await delay(200);
          assert.equal(quitting, false, "error window must survive the splash handoff");
          assert.equal(BrowserWindow.getAllWindows().length, 1);
          const text = await win.webContents.executeJavaScript("document.body.innerText");
          report({ errorWindow: true, text, kills });
          win.close();
        } else if (scenario === "companion" && url.endsWith("?surface=panel")) {
          await delay(250);
          assert.equal(BrowserWindow.getAllWindows().length, 2);
          const panel = win;
          const robot = BrowserWindow.getAllWindows().find(other => other.webContents.getURL().endsWith("?surface=robot"));
          assert.ok(robot);
          assert.equal(robot.webContents.session, panel.webContents.session);
          const run = (window, script) => window.webContents.executeJavaScript(script);
          await run(panel, "window.received=[]; window.focusDesktop.patrol.onContent(value=>window.received.push(value)); window.focusDesktop.patrol.ready()");
          const ready = await run(robot, "window.focusDesktop.patrol.ready()");
          assert.equal(ready.limits.general_file_bytes, 1024);
          await run(robot, "window.focusDesktop.patrol.receive({items:[{kind:'file',name:'clipboard.png',type:'image/png',bytes:new Uint8Array([1,2,3])}]})");
          await delay(30);
          assert.equal(await run(panel, "received[0].items[0].bytes.byteLength"), 3);
          await assert.rejects(run(panel, "window.focusDesktop.patrol.receive({items:[{kind:'text',text:'wrong sender'}]})"), /不能调用/);
          const beforeFocus = BrowserWindow.getFocusedWindow();
          await run(panel, "window.focusDesktop.patrol.setState({workspaceId:'fixture',workspaceName:'Fixture',state:'working',dirty:true,pending:false})");
          assert.equal(BrowserWindow.getFocusedWindow(), beforeFocus);
          panel.close();
          assert.equal(panel.isDestroyed(), false);
          await run(robot, "window.focusDesktop.patrol.openPanel()");
          assert.equal(BrowserWindow.getAllWindows().length, 2);
          await run(panel, "window.focusDesktop.patrol.openWorkbench({workspaceId:'fixture'})");
          const workbench = BrowserWindow.getAllWindows().find(other => other.webContents.getURL().endsWith("/desktop/"));
          assert.ok(workbench);
          await run(workbench, "window.opened=null; window.focusDesktop.patrol.onWorkbench(value=>window.opened=value); window.focusDesktop.patrol.ready()");
          await delay(30);
          assert.equal(await run(workbench, "window.opened.workspaceId"), "fixture");
          workbench.close();
          assert.equal(workbench.isDestroyed(), false);
          const second = nativeSpawn(process.execPath, [__filename], {
            windowsHide: true, env: { ...process.env, FOCUS_STARTUP_TEST: "secondary" }, stdio: "ignore",
          });
          const secondWatchdog = setTimeout(() => second.kill(), 8000);
          const secondCode = await new Promise((resolve, reject) => { second.once("exit", resolve); second.once("error", reject); });
          clearTimeout(secondWatchdog);
          assert.equal(secondCode, 0);
          assert.equal(secondInstances, 1);
          assert.equal(backendStarts, 1);
          assert.equal(BrowserWindow.getAllWindows().length, 3);
          let prompts = 0;
          require("electron").dialog.showMessageBox = async () => { prompts++; return { response: 0 }; };
          app.quit();
          await delay(30);
          assert.notEqual(app.isQuitting, true);
          assert.equal(prompts, 1);
          assert.equal(kills, 0);
          await run(panel, "window.focusDesktop.patrol.setState({workspaceId:'fixture',workspaceName:'Fixture',state:'idle',dirty:false,pending:false})");
          report({ companion: true, backendStarts, secondInstances, windows: 3, prompts });
          app.quit();
        } else if (scenario === "healthy" && url.endsWith("/desktop/")) {
          await delay(200);
          // windowsHide suppresses native visibility; assert the loaded main window survives instead.
          assert.equal(quitting, false);
          assert.equal(BrowserWindow.getAllWindows().length, 1);
          report({ mainWindow: true, healthRequests });
          win.close();
        }
      } catch (error) { fail(error); }
    });
  });
  require("./main.cjs");
} else {
  const { test } = require("node:test");
  const { spawn } = require("node:child_process");
  const fs = require("node:fs");
  const os = require("node:os");
  const path = require("node:path");

  async function launch(scenario) {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), "focus-startup-test-"));
    assert.ok(path.resolve(home).startsWith(path.resolve(os.tmpdir()) + path.sep));
    let stdout = "", stderr = "";
    const env = { ...process.env, FOCUS_STARTUP_TEST: scenario,
        FOCUS_GLOBAL_HOME: home, FOCUS_ELECTRON_USER_DATA: path.join(home, "electron"),
        FOCUS_DISABLE_HARDWARE_ACCELERATION: "1", FOCUS_DESKTOP_PATROL: scenario === "companion" ? "1" : "0" };
    delete env.ELECTRON_RUN_AS_NODE;
    const child = spawn(require("electron"), [__filename], {
      windowsHide: true, env,
      stdio: ["ignore", "pipe", "pipe"],
    });
    child.stdout.on("data", value => { stdout += value; });
    child.stderr.on("data", value => { stderr += value; });
    const watchdog = setTimeout(() => child.kill(), 45000);
    try {
      const code = await new Promise((resolve, reject) => { child.once("error", reject); child.once("exit", resolve); });
      const events = stdout.split(/\r?\n/).filter(line => line.startsWith("STARTUP_TEST ")).map(line => JSON.parse(line.slice(13)));
      return { code, events, stderr };
    } finally {
      clearTimeout(watchdog);
      fs.rmSync(home, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
    }
  }

  for (const [scenario, message, kills] of [
    ["preflight", /Docker Compose 检查失败/, 0],
    ["backend-exit", /代码 7/, 0],
    ["spawn-error", /Python spawn failed/, 1],
    ["hung-health", /30 秒.*健康检查/, 1],
  ]) {
    test(`startup failure stays visible and exits nonzero: ${scenario}`, { timeout: 50000 }, async () => {
      const result = await launch(scenario);
      assert.equal(result.code, 1, result.stderr);
      const error = result.events.find(event => event.errorWindow);
      assert.ok(error, "native error window must finish loading before exit");
      assert.match(error.text, message);
      assert.equal(error.kills, kills);
      assert.match(result.stderr, /Focus 启动失败/);
    });
  }
  test("slow successful startup keeps the main window and exits normally", async () => {
    const result = await launch("healthy");
    assert.equal(result.code, 0, result.stderr);
    assert.ok(result.events.some(event => event.mainWindow));
    assert.ok(result.events.find(event => event.mainWindow).healthRequests > 1);
    assert.doesNotMatch(result.stderr, /Focus 启动失败/);
  });
  test("user quit cancels startup without opening an error window", async () => {
    const result = await launch("cancel");
    assert.equal(result.code, 0, result.stderr);
    assert.equal(result.events.some(event => event.errorWindow), false);
    assert.equal(result.events.find(event => "backendKills" in event).backendKills, 1);
    assert.doesNotMatch(result.stderr, /Focus 启动失败/);
  });
  test("Windows companion keeps one backend and restores existing windows through native IPC", { skip: process.platform !== "win32" }, async () => {
    const result = await launch("companion");
    assert.equal(result.code, 0, result.stderr);
    assert.ok(result.events.some(event => event.companion));
    assert.equal(result.events.find(event => "backendKills" in event).backendKills, 1);
  });
}
