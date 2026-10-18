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
  let server, childBackend, kills = 0, quitting = false, healthRequests = 0, backendStarts = 0, secondInstances = 0;
  const launches = [];
  let rejectReconcile = false;
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
  cp.spawn = (_command, args, options) => {
    backendStarts++;
    launches.push({ args, session: options.env.FOCUS_DESKTOP_SESSION });
    const backend = Object.assign(new EventEmitter(), {
      stdout: new PassThrough(), stderr: new PassThrough(), killed: false,
      kill() { this.killed = true; kills++; server?.close(); setImmediate(() => this.emit("exit", null, "SIGTERM")); },
    });
    childBackend = backend;
    const readyAt = Date.now() + 750;
    server = http.createServer((request, response) => {
      if (request.url === "/health") healthRequests++;
      if ((["healthy", "companion", "runtime-exit"].includes(scenario) || scenario.startsWith("preferences-")) && Date.now() >= readyAt) {
        if (request.url === "/desktop/api/materials/limits") {
          assert.equal(request.headers["x-focus-session"], process.env.FOCUS_DESKTOP_SESSION);
          response.setHeader("Content-Type", "application/json");
          response.end(JSON.stringify({ general_file_bytes: 1024, run_material_count: 4 }));
        } else if (request.url === "/desktop/api/settings/models") {
          assert.equal(request.headers["x-focus-session"], process.env.FOCUS_DESKTOP_SESSION);
          response.setHeader("Content-Type", "application/json");
          response.end(JSON.stringify({ setup_required: false }));
        } else if (request.url === "/desktop/api/reconcile") {
          assert.equal(request.headers["x-focus-session"], launches[0].session);
          if (rejectReconcile) { rejectReconcile = false; response.writeHead(503); response.end("unavailable"); return; }
          response.setHeader("Content-Type", "application/json");
          response.end('{"ok":true}');
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
          await run(panel, "window.received=[]; window.focusDesktop.patrol.onContent(value=>window.received.push(value)); window.focusDesktop.patrol.recovery.onFlush(token=>window.focusDesktop.patrol.recovery.flushed({token})); window.focusDesktop.patrol.ready()");
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
          await run(panel, "window.pendingDraft='机器人面板未发送草稿'");
          server.closeAllConnections();
          await new Promise(resolve => server.close(resolve));
          childBackend.emit("exit", 9, null);
          await delay(50);
          assert.equal(BrowserWindow.getAllWindows().length, 3, "运行期失联保留机器人、面板和工作台");
          assert.equal(await run(panel, "pendingDraft"), "机器人面板未发送草稿");
          await run(panel, "window.focusDesktop.gateway.restart()");
          assert.equal(BrowserWindow.getAllWindows().length, 3);
          const second = nativeSpawn(process.execPath, [__filename], {
            windowsHide: true, env: { ...process.env, FOCUS_STARTUP_TEST: "secondary" }, stdio: "ignore",
          });
          const secondWatchdog = setTimeout(() => second.kill(), 8000);
          const secondCode = await new Promise((resolve, reject) => { second.once("exit", resolve); second.once("error", reject); });
          clearTimeout(secondWatchdog);
          assert.equal(secondCode, 0);
          assert.equal(secondInstances, 1);
          assert.equal(backendStarts, 2);
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
        } else if (scenario.startsWith("preferences-") && url.endsWith("/desktop/")) {
          await delay(250);
          const prefs = { "focus-interface-language-v1": "en-US", "focus-zoom-level": "2", "focus-shell-layout": '{"navWidth":250,"inspectorWidth":360,"navCollapsed":true}' };
          if (scenario === "preferences-write") {
            await win.webContents.executeJavaScript(`for (const [key,value] of Object.entries(${JSON.stringify(prefs)})) window.focusDesktop.preferences.setItem(key,value)`);
          } else {
            const restored = await win.webContents.executeJavaScript(`Object.fromEntries(${JSON.stringify(Object.keys(prefs))}.map(key=>[key,window.focusDesktop.preferences.getItem(key)]))`);
            assert.deepEqual(restored, prefs);
          }
          report({ preferences: true, origin: new URL(url).origin });
          app.quit();
        } else if (scenario === "runtime-exit" && url.endsWith("/desktop/")) {
          await delay(250);
          await win.webContents.executeJavaScript("document.body.innerHTML='<textarea id=work></textarea>'; work.value='未发送草稿'; work.setSelectionRange(2,4); window.readingPosition=123");
          await win.webContents.executeJavaScript(require("node:fs").readFileSync(require.resolve("./gateway-recovery.js"), "utf8"));
          server.closeAllConnections();
          await new Promise(resolve => server.close(resolve));
          childBackend.emit("exit", 7, null);
          await delay(100);
          assert.equal(win.isDestroyed(), false, "运行期后端退出必须保留工作台");
          assert.equal(await win.webContents.executeJavaScript("work.value"), "未发送草稿");
          assert.equal(await win.webContents.executeJavaScript("readingPosition"), 123);
          assert.equal(await win.webContents.executeJavaScript("work.selectionStart"), 2);
          assert.equal(await win.webContents.executeJavaScript("document.querySelector('.gateway-recovery').hidden"), false);
          assert.match(await win.webContents.executeJavaScript("document.querySelector('.gateway-recovery').innerText"), /重新连接.*重启服务/s);
          assert.equal(await win.webContents.executeJavaScript("window.focusDesktop.gateway.status().then(s=>s.status)"), "unavailable");
          // 并发操作合并为同一次恢复，保持页面、来源、令牌和已有草稿。
          await win.webContents.executeJavaScript("Promise.all([window.focusDesktop.gateway.restart(),window.focusDesktop.gateway.restart()])");
          assert.equal(backendStarts, 2);
          assert.deepEqual(launches[1], launches[0]);
          const revision = await win.webContents.executeJavaScript("window.focusDesktop.gateway.status().then(s=>s.revision)");
          await win.webContents.executeJavaScript("window.focusDesktop.gateway.reconnect()");
          assert.equal(backendStarts, 2, "重连不额外启动进程");
          await win.webContents.executeJavaScript("window.focusDesktop.gateway.restart()");
          assert.equal(backendStarts, 3, "在运行的服务也可重启，先等旧进程退出");
          assert.equal(kills, 1);
          assert.ok(await win.webContents.executeJavaScript("window.focusDesktop.gateway.status().then(s=>s.revision)") > revision);
          assert.equal(await win.webContents.executeJavaScript("work.value"), "未发送草稿");
          assert.equal(await win.webContents.executeJavaScript("readingPosition"), 123);
          assert.equal(await win.webContents.executeJavaScript("work.selectionStart"), 2);
          assert.equal(win.webContents.getURL(), url);
          assert.equal(await win.webContents.executeJavaScript("document.querySelector('.gateway-recovery').hidden"), true);
          rejectReconcile = true;
          await assert.rejects(win.webContents.executeJavaScript("window.focusDesktop.gateway.reconnect()"), /HTTP 503/);
          assert.equal(win.isDestroyed(), false);
          assert.equal(await win.webContents.executeJavaScript("document.querySelector('.gateway-recovery').hidden"), false);
          await win.webContents.executeJavaScript(`new Promise(resolve => {
            const stop = window.focusDesktop.gateway.onStatus(value => { if (value.status === 'ready') { stop(); resolve(); } });
            document.querySelector('.gateway-recovery button').click();
          })`);
          assert.equal(await win.webContents.executeJavaScript("document.querySelector('.gateway-recovery').hidden"), true);
          report({ runtimePreserved: true, restarts: 2 });
          app.quit();
        } else if (scenario === "healthy" && url.endsWith("/desktop/")) {
          await delay(200);
          // windowsHide suppresses native visibility; assert the loaded main window survives instead.
          assert.equal(quitting, false);
          assert.equal(BrowserWindow.getAllWindows().length, 1);
          const recovered = await win.webContents.executeJavaScript(`(async () => {
            const recovery=window.focusDesktop.workspacePatrolRecovery;
            recovery.onFlush(token=>recovery.flushed({token}));
            recovery.read();
            await recovery.put('fixture',new Uint8Array([0,255,128]));
            recovery.checkpoint({version:1,workspaces:[],source_ids:['fixture'],binding:{workspace_id:'saved',path:'/saved'}});
            return {enabled:window.focusDesktop.patrol.enabled,binding:recovery.read().state.binding,bytes:Array.from(recovery.read().sources[0][1])};
          })()`);
          assert.equal(recovered.enabled,false);
          assert.equal(recovered.binding.workspace_id,'saved');
          assert.deepEqual(recovered.bytes,[0,255,128]);
          report({ mainWindow: true, healthRequests, workbenchRecovery: true });
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

  async function launch(scenario, sharedHome) {
    const home = sharedHome || fs.mkdtempSync(path.join(os.tmpdir(), "focus-startup-test-"));
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
      if (!sharedHome) fs.rmSync(home, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
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
    assert.equal(result.events.find(event => event.mainWindow).workbenchRecovery,true);
    assert.doesNotMatch(result.stderr, /Focus 启动失败/);
  });
  test("运行中 Gateway 退出保留原窗口、草稿和阅读位置", async () => {
    const result = await launch("runtime-exit");
    assert.equal(result.code, 0, result.stderr);
    assert.ok(result.events.some(event => event.runtimePreserved));
  });
  test("完全退出后换端口，语言、缩放和分栏偏好仍从同一 userData 恢复", async () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), "focus-startup-test-prefs-"));
    const previousPort = require("node:net").createServer();
    try {
      const first = await launch("preferences-write", home);
      assert.equal(first.code, 0, first.stderr);
      const saved = first.events.find(event => event.preferences);
      assert.ok(saved);
      // 占住上次端口，确保本次验收确实跨来源，而不依赖 OS 的端口随机分配。
      await new Promise((resolve, reject) => {
        previousPort.once("error", reject);
        previousPort.listen(Number(new URL(saved.origin).port), "127.0.0.1", resolve);
      });
      const second = await launch("preferences-read", home);
      assert.equal(second.code, 0, second.stderr);
      const restored = second.events.find(event => event.preferences);
      assert.ok(saved && restored);
      assert.notEqual(saved.origin, restored.origin);
    } finally {
      if (previousPort.listening) await new Promise(resolve => previousPort.close(resolve));
      assert.ok(path.resolve(home).startsWith(path.resolve(os.tmpdir()) + path.sep));
      fs.rmSync(home, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
    }
  });
  test("user quit cancels startup without opening an error window", async () => {
    const result = await launch("cancel");
    assert.equal(result.code, 0, result.stderr);
    assert.equal(result.events.some(event => event.errorWindow), false);
    assert.equal(result.events.find(event => "backendKills" in event).backendKills, 1);
    assert.doesNotMatch(result.stderr, /Focus 启动失败/);
  });
  test("Windows companion restores existing windows and restarts Gateway without losing panel state", { skip: process.platform !== "win32" }, async () => {
    const result = await launch("companion");
    assert.equal(result.code, 0, result.stderr);
    assert.ok(result.events.some(event => event.companion));
    assert.equal(result.events.find(event => "backendKills" in event).backendKills, 1);
  });
}
