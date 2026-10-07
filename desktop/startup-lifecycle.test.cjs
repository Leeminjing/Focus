// Run the real main process and native windows; only external Python/Docker are fixtures.
const assert = require("node:assert/strict");
const { setTimeout: delay } = require("node:timers/promises");

if (process.versions.electron) {
  const { app, BrowserWindow } = require("electron");
  const cp = require("node:child_process");
  const { EventEmitter } = require("node:events");
  const { PassThrough } = require("node:stream");
  const http = require("node:http");
  const scenario = process.env.FOCUS_STARTUP_TEST;
  let server, kills = 0, quitting = false, healthRequests = 0;
  const report = value => console.log(`STARTUP_TEST ${JSON.stringify(value)}`);
  const fail = error => { console.error(error); server?.close(); app.exit(90); };

  cp.execFileSync = (command, args) => {
    if (scenario === "preflight" && command === "docker") throw new Error("fixture: Docker unavailable");
    return command === "where.exe" ? `${process.execPath}\n` : "fixture";
  };
  cp.spawn = (_command, args) => {
    const backend = Object.assign(new EventEmitter(), {
      stdout: new PassThrough(), stderr: new PassThrough(), killed: false,
      kill() { this.killed = true; kills++; server?.close(); setImmediate(() => this.emit("exit", null, "SIGTERM")); },
    });
    const readyAt = Date.now() + 750;
    server = http.createServer((request, response) => {
      if (request.url === "/health") healthRequests++;
      if (scenario === "healthy" && Date.now() >= readyAt) { response.end("ok"); }
      else if (scenario !== "hung-health" && scenario !== "cancel") { response.writeHead(503); response.end("starting"); }
    });
    server.listen(Number(args[args.indexOf("--port") + 1]), "127.0.0.1");
    if (scenario === "backend-exit") setTimeout(() => { server.close(); backend.emit("exit", 7, null); }, 100);
    if (scenario === "spawn-error") setTimeout(() => backend.emit("error", new Error("fixture: Python spawn failed")), 100);
    if (scenario === "cancel") setTimeout(() => app.quit(), 250);
    return backend;
  };
  app.on("before-quit", () => { quitting = true; report({ quitting: true, kills }); server?.close(); });
  app.on("will-quit", () => report({ backendKills: kills }));
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
        FOCUS_DISABLE_HARDWARE_ACCELERATION: "1" };
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
    ["preflight", /Docker unavailable/, 0],
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
}
