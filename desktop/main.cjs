/*
 * 本文件启动 Focus 单实例桌面运行时。输入为 Python/Git/Docker、品牌资源和环境变量，输出为
 * 单一 loopback FastAPI、同源隔离窗口及 Windows 桌面 Patrol 宿主；失败时切换到品牌错误窗口。
 * 工作流为先取单实例锁，隐藏预检控制台后启动及检查后端，Windows 默认显示机器人，工作台按需打开、关闭仅隐藏；
 * 明确退出时检查未确认输入，再沿用后端 shutdown。其他平台或 FOCUS_DESKTOP_PATROL=0 保留工作台启动。
 * 工作台缩放以 Chromium 每级 1.2 倍钳制在 50%~300%，原生标题覆盖层同步 56 CSS px 基础高度。
 * 示例：electron . 启动机器人，托盘打开工作台；第二实例唤回既有机器人而不再启动后端。
 */
const { app, BrowserWindow, dialog, ipcMain, Menu, shell, nativeTheme, Notification } = require("electron");
const { TaskNotifications } = require("./task-notifications.cjs");
const { supportsAcrylic, bindWindowMaterial } = require("./window-material.cjs");
const { DesktopPatrolHost } = require("./desktop-patrol-host.cjs");
const nativeMaterialSupported = supportsAcrylic(process.platform, require("node:os").release());
const { spawn } = require("node:child_process");
const { createStartupRuntime, createStageRecorder } = require("./startup-runtime.cjs");
const crypto = require("node:crypto");
const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");
const { setTimeout: delay } = require("node:timers/promises");

const desktopDir = __dirname;
const rootDir = path.resolve(desktopDir, "..");
const focusIconPath = path.join(desktopDir, "assets", process.platform === "win32" ? "focus-icon.ico" : "focus-icon.png");

const ZOOM_LEVEL_MIN = -3;
const ZOOM_LEVEL_MAX = 5;
const TITLEBAR_BASE_HEIGHT = 56;
const patrolEnabled = process.platform === "win32" && process.env.FOCUS_DESKTOP_PATROL !== "0";
let revealPatrolPanel = process.argv.includes("--show-patrol-panel");

function clampZoomLevel(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 0;
  return Math.min(ZOOM_LEVEL_MAX, Math.max(ZOOM_LEVEL_MIN, Math.round(numeric)));
}

function zoomFactorForLevel(level) {
  return 1.2 ** clampZoomLevel(level);
}

function syncTitleBarOverlay(win, level) {
  if (process.platform === "darwin" || !win || win.isDestroyed?.()) return;
  const height = Math.round(TITLEBAR_BASE_HEIGHT * zoomFactorForLevel(level));
  try { win.setTitleBarOverlay({ height }); } catch {}
}

function ensureFocusHome() {
  const home = process.env.FOCUS_GLOBAL_HOME || path.join(app.getPath("home"), ".focus");
  fs.mkdirSync(home, { recursive: true });
  fs.mkdirSync(path.join(home, "plugins"), { recursive: true });
  fs.mkdirSync(path.join(home, "users"), { recursive: true });
  const extensions = path.join(home, "extensions_config.json");
  if (!fs.existsSync(extensions)) fs.writeFileSync(extensions, '{"mcpServers": {}}', "utf8");
  const config = path.join(home, "config.yaml");
  if (!fs.existsSync(config)) fs.writeFileSync(config, "# Focus 全局配置默认层（仓库/项目态优先）\n", "utf8");
  return home;
}
let backend = null;
let splashWindow = null;
let mainWindow = null;
let patrolHost = null;
let desktopOrigin = null;
let workbenchLoading = null;
let fatalErrorShown = false;
let startupStage = "正在加载启动窗口";
let taskNotifications = null;
let notificationTarget = null;
let notificationReadyContents = null;
const activeNotifications = new Set();
const startupAbort = new AbortController();

if (process.env.FOCUS_DISABLE_HARDWARE_ACCELERATION === "1") {
  app.disableHardwareAcceleration();
  app.commandLine.appendSwitch("disable-gpu");
}
if (process.env.FOCUS_ELECTRON_USER_DATA) app.setPath("userData", process.env.FOCUS_ELECTRON_USER_DATA);

function pythonEnvironment(session) {
  const separator = process.platform === "win32" ? ";" : ":";
  const pythonPath = [path.join(rootDir, "backend", "packages", "harness"), rootDir, process.env.PYTHONPATH].filter(Boolean).join(separator);
  return {
    ...process.env,
    PYTHONPATH: pythonPath,
    JWT_SECRET: process.env.JWT_SECRET || "desktop-unused",
    FOCUS_DESKTOP_SESSION: session,
    FOCUS_DATABASE_URL: process.env.FOCUS_DATABASE_URL || "postgresql+asyncpg://focus:qweasdzxc123@127.0.0.1:7221/focus",
    PYTHONUTF8: "1",
  };
}

function createSplashWindow() {
  splashWindow = new BrowserWindow({
    show: false,
    width: 440,
    height: 340,
    frame: false,
    transparent: true,
    resizable: false,
    maximizable: false,
    minimizable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    backgroundColor: "#00000000",
    icon: focusIconPath,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  splashWindow.once("ready-to-show", () => splashWindow?.show());
  return splashWindow.loadFile(path.join(desktopDir, "splash.html"));
}

async function updateSplashStage(label, progress) {
  startupStage = label;
  if (!splashWindow || splashWindow.isDestroyed()) return;
  await splashWindow.webContents.executeJavaScript(
    `window.setFocusSplashStage(${JSON.stringify(label)}, ${Number(progress) || 0})`,
  ).catch(() => {});
}

function closeSplashWindow() {
  if (splashWindow && !splashWindow.isDestroyed()) splashWindow.destroy();
  splashWindow = null;
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const port = server.address().port;
      server.close(() => resolve(port));
    });
  });
}

async function waitForHealth(url, timeoutMs = 30000) {
  const signal = startupAbort.signal;
  const deadline = Date.now() + timeoutMs;
  let lastFailure = "尚未收到响应";
  for (let remaining; (remaining = deadline - Date.now()) > 0;) {
    signal.throwIfAborted();
    try {
      const response = await fetch(`${url}/health`, {
        signal: AbortSignal.any([signal, AbortSignal.timeout(Math.min(1000, remaining))]),
      });
      if (response.ok) return;
      lastFailure = `HTTP ${response.status}`;
    } catch (error) {
      signal.throwIfAborted();
      lastFailure = error.message;
    }
    await delay(Math.min(350, Math.max(0, deadline - Date.now())), undefined, { signal });
  }
  throw new Error(`FastAPI 在 ${timeoutMs / 1000} 秒内未通过健康检查（${lastFailure}）`);
}

function errorWindow(error) {
  const win = new BrowserWindow({ width: 760, height: 460, minWidth: 560, minHeight: 380, backgroundColor: "#f5f7fa", icon: focusIconPath });
  const message = String(error?.message || error).replace(/[&<>]/g, value => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[value]);
  const page = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Focus 启动失败</title><style>
    :root{font-family:"Segoe UI","Microsoft YaHei UI",sans-serif;color:#172033;background:#f5f7fa}
    *{box-sizing:border-box}body{min-height:100vh;margin:0;display:grid;place-items:center;padding:32px}
    main{width:min(620px,100%);padding:36px;border:1px solid #dce2ea;border-radius:16px;background:#fff;box-shadow:0 18px 48px rgba(21,35,55,.09)}
    small{color:#b42318;font-weight:700;letter-spacing:.1em}h1{margin:8px 0 12px;font-size:28px}p{color:#596579;line-height:1.65}
    pre{overflow:auto;padding:14px;border-radius:10px;background:#f7f8fa;color:#9b2c24;white-space:pre-wrap;overflow-wrap:anywhere}
    button{min-height:38px;margin-top:10px;padding:0 18px;border:1px solid #cbd3df;border-radius:9px;background:#fff;color:#172033;font:inherit;cursor:pointer}
    button:focus-visible{outline:3px solid rgba(11,108,245,.28);outline-offset:2px}
  </style><body><main><small>STARTUP ERROR</small><h1>Focus 未能启动</h1><p>本地服务尚未就绪。请确认 Python、Git、Docker Desktop 和项目 Python 依赖均已安装并可运行。</p><pre>${message}</pre><button onclick="window.close()">关闭 Focus</button></main></body></html>`;
  win.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(page)}`).catch(error => {
    console.error("Focus 错误窗口加载失败:", error);
    app.quit();
  });
  return win;
}

function stopBackend() {
  const child = backend;
  backend = null;
  if (child && !child.killed) child.kill();
}

function showFatalError(error) {
  if (fatalErrorShown || app.isQuitting) return;
  fatalErrorShown = true;
  console.error(`Focus 启动失败（${startupStage}）:`, error);
  startupAbort.abort(error);
  stopBackend();
  try {
    errorWindow(new Error(`${startupStage}: ${error?.message || error}`));
  } catch (windowError) {
    console.error("Focus 错误窗口创建失败:", windowError);
    app.quit();
    return;
  }
  closeSplashWindow();
  patrolHost?.dispose();
  patrolHost = null;
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.destroy();
  mainWindow = null;
}

function externalHttpUrl(value) {
  try {
    const url = new URL(String(value));
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
  } catch { return null; }
}

function protectAppNavigation(win, apiBase) {
  win.webContents.setWindowOpenHandler(({ url }) => {
    const external = externalHttpUrl(url);
    if (external) shell.openExternal(external).catch(() => {});
    return { action: "deny" };
  });
  win.webContents.on("will-navigate", (event, target) => {
    try {
      if (new URL(target).origin === new URL(apiBase).origin) return;
    } catch {}
    event.preventDefault();
    const external = externalHttpUrl(target);
    if (external) shell.openExternal(external).catch(() => {});
  });
}

async function start() {
  Menu.setApplicationMenu(null);
  const startupId = crypto.randomUUID();
  const home = ensureFocusHome();
  const logPath = path.join(home, "startup-timing.jsonl");
  // 每次启动保留结构化阶段耗时，最多保留约 1 MiB；无连接串或密钥。
  try { if (fs.statSync(logPath).size > 1024 * 1024) fs.renameSync(logPath, `${logPath}.previous`); } catch {}
  const recorder = createStageRecorder({
    update: updateSplashStage,
    report: entry => {
      const line = JSON.stringify({ startup: startupId, at: new Date().toISOString(), ...entry });
      console.log(`[focus-startup] ${line}`);
      try { fs.appendFileSync(logPath, `${line}\n`, "utf8"); } catch (error) { console.error("无法写入启动耗时:", error.message); }
    },
  });
  const stage = recorder.stage;
  await stage("splash", "正在加载启动窗口", 0, () => createSplashWindow());
  startupAbort.signal.throwIfAborted();
  const session = crypto.randomBytes(32).toString("hex");
  const env = pythonEnvironment(session);
  const managed = require("../scripts/managed-update.cjs").assertReady(rootDir, home);
  const runtime = createStartupRuntime({ rootDir, home, signal: startupAbort.signal, stage, managed });
  const pythonRuntime = await stage("runtime", "正在读取已准备的运行环境", 10, () => runtime.prepare());
  await runtime.prepareDatabase(pythonRuntime, env);
  const port = await freePort();
  startupAbort.signal.throwIfAborted();
  const apiBase = `http://127.0.0.1:${port}`;
  desktopOrigin = apiBase;
  process.env.FOCUS_DESKTOP_API = apiBase;
  process.env.FOCUS_DESKTOP_SESSION = session;
  await updateSplashStage("正在启动 Focus Gateway", 66);
  backend = spawn(pythonRuntime.command, [...pythonRuntime.prefix, "-m", "uvicorn", "backend.app.gateway.app:app", "--host", "127.0.0.1", "--port", String(port), "--loop", "backend.app.gateway.app:selector_loop_factory"], {
    cwd: rootDir,
    env: { ...env, FOCUS_DESKTOP_PORT: String(port) },
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
  });
  backend.stdout.on("data", data => process.stdout.write(data));
  backend.stderr.on("data", data => process.stderr.write(data));
  backend.once("error", showFatalError);
  backend.once("exit", (code, signal) => {
    backend = null;
    if (!app.isQuitting) showFatalError(new Error(`FastAPI 已退出（代码 ${code ?? "—"}${signal ? `，信号 ${signal}` : ""}）`));
  });
  await stage("gateway-health", "正在等待 Focus Gateway 就绪", 72, () => waitForHealth(apiBase));
  await stage("interface", "正在加载工作区", 84, async () => {
    startupAbort.signal.throwIfAborted();
    if (patrolEnabled) {
      const response = await fetch(`${apiBase}/desktop/api/materials/limits`, {
        headers: { "X-Focus-Session": session }, signal: AbortSignal.any([startupAbort.signal, AbortSignal.timeout(10000)]),
      });
      if (!response.ok) throw new Error(`读取材料资源限制失败：HTTP ${response.status}`);
      const limits = await response.json();
      if (!Number.isSafeInteger(limits.general_file_bytes) || limits.general_file_bytes <= 0
        || !Number.isSafeInteger(limits.run_material_count) || limits.run_material_count <= 0) throw new Error("材料资源限制无效");
      patrolHost = new DesktopPatrolHost({
        electron: require("electron"), apiBase, desktopDir, icon: focusIconPath, limits,
        openWorkbench: showWorkbench, getWorkbench: () => mainWindow, quit: () => app.quit(),
      });
      await patrolHost.start();
      if (revealPatrolPanel) patrolHost.openPanel();
    } else {
      await showWorkbench();
    }
  });
  await stage("ready", "Focus 已就绪", 100, async () => {});
  startupAbort.signal.throwIfAborted();
  closeSplashWindow();
  const notificationFile = path.join(app.getPath("userData"), "task-notifications.json");
  taskNotifications = new TaskNotifications({
    load: () => { try { return JSON.parse(fs.readFileSync(notificationFile, "utf8")); } catch { return {}; } },
    save: value => {
      fs.mkdirSync(path.dirname(notificationFile), { recursive: true });
      fs.writeFileSync(`${notificationFile}.tmp`, JSON.stringify(value), "utf8");
      fs.renameSync(`${notificationFile}.tmp`, notificationFile);
    },
    listTasks: async () => {
      const response = await fetch(`${apiBase}/desktop/api/tasks`, {
        headers: { "X-Focus-Session": session }, signal: AbortSignal.timeout(10000),
      });
      if (!response.ok) throw new Error(`读取任务状态失败: HTTP ${response.status}`);
      return response.json();
    },
    onTasks: tasks => {
      if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send("focus:tasks-updated", tasks);
    },
    show: event => {
      if (!Notification.isSupported()) return;
      const notification = new Notification({ title: event.title, body: event.body, icon: focusIconPath });
      activeNotifications.add(notification);
      notification.on("close", () => activeNotifications.delete(notification));
      notification.on("failed", (_event, error) => { activeNotifications.delete(notification); console.warn("桌面提醒不可用:", error); });
      notification.on("click", () => {
        notificationTarget = event.target;
        void showWorkbench().then(flushNotificationTarget).catch(error => console.warn("打开提醒任务失败:", error.message));
      });
      notification.show();
    },
  });
  taskNotifications.start();
}

function flushNotificationTarget() {
  if (!notificationTarget || !mainWindow || mainWindow.isDestroyed()
      || notificationReadyContents !== mainWindow.webContents) return;
  mainWindow.webContents.send("focus:notification-open", notificationTarget);
  notificationTarget = null;
}

async function showWorkbench() {
  if (workbenchLoading) await workbenchLoading;
  if (!mainWindow || mainWindow.isDestroyed()) {
    workbenchLoading = createWorkbench();
    try { await workbenchLoading; } finally { workbenchLoading = null; }
  }
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

async function createWorkbench() {
  const mainWindowOptions = {
    show: false,
    width: 1440,
    height: 1024,
    minWidth: 900,
    minHeight: 680,
    autoHideMenuBar: true,
    titleBarStyle: "hidden",
    backgroundColor: nativeMaterialSupported ? "#00000000" : "#f3f6fb",
    ...(nativeMaterialSupported ? { backgroundMaterial: "acrylic" } : {}),
    icon: focusIconPath,
    webPreferences: {
      preload: path.join(desktopDir, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      additionalArguments: [
        ...(nativeMaterialSupported ? ["--focus-native-material=acrylic"] : []),
        ...(patrolEnabled ? ["--focus-desktop-patrol"] : []),
      ],
    },
  };
  if (process.platform !== "darwin") {
    mainWindowOptions.titleBarOverlay = {
      color: "#ffffff",
      symbolColor: "#18202d",
      height: TITLEBAR_BASE_HEIGHT,
    };
  }
  mainWindow = new BrowserWindow(mainWindowOptions);
  mainWindow.webContents.on("did-start-loading", () => { notificationReadyContents = null; });
  nativeTheme.themeSource = "light";
  bindWindowMaterial(mainWindow, nativeTheme, nativeMaterialSupported);
  mainWindow.setMenuBarVisibility(false);
  protectAppNavigation(mainWindow, desktopOrigin);
  mainWindow.on("close", event => {
    if (patrolHost && !app.isQuitting) { event.preventDefault(); mainWindow.hide(); }
  });
  mainWindow.on("closed", () => { mainWindow = null; });
  await mainWindow.loadURL(`${desktopOrigin}/desktop/`);
}

function assertDesktopSender(event) {
  if (patrolHost?.accepts(event)) return;
  if (mainWindow && !mainWindow.isDestroyed() && event.sender === mainWindow.webContents && event.senderFrame === event.sender.mainFrame) {
    try { if (new URL(event.senderFrame.url).origin === desktopOrigin) return; } catch {}
  }
  throw new Error("仅允许 Focus 同源窗口调用桌面功能");
}

ipcMain.handle("focus:select-workspace", async event => {
  assertDesktopSender(event);
  const result = await dialog.showOpenDialog(BrowserWindow.fromWebContents(event.sender), { properties: ["openDirectory", "createDirectory"] });
  return result.canceled ? "" : result.filePaths[0];
});

ipcMain.handle("focus:notifications-settings", (event, enabled) => {
  assertDesktopSender(event);
  if (enabled === undefined) return taskNotifications?.settings() || { enabled: true };
  if (!taskNotifications) throw new Error("桌面提醒仍在启动，请稍后重试");
  const settings = taskNotifications.configure(enabled);
  if (!enabled) for (const notification of activeNotifications) notification.close();
  return settings;
});

ipcMain.handle("focus:notifications-ready", event => {
  assertDesktopSender(event);
  if (event.sender !== mainWindow?.webContents) return;
  notificationReadyContents = event.sender;
  flushNotificationTarget();
});

ipcMain.handle("focus:open-external", async (event, value) => {
  assertDesktopSender(event);
  const url = externalHttpUrl(value);
  if (!url) throw new Error("仅允许打开 HTTP(S) 外部链接");
  await shell.openExternal(url);
  return true;
});

ipcMain.handle("focus:set-zoom-level", (event, value) => {
  assertDesktopSender(event);
  const level = clampZoomLevel(value);
  const win = BrowserWindow.fromWebContents(event.sender);
  if (!win || win.isDestroyed()) return level;
  win.webContents.setZoomLevel(level);
  syncTitleBarOverlay(win, level);
  return level;
});

ipcMain.handle("focus:get-zoom-level", event => {
  assertDesktopSender(event);
  const contents = BrowserWindow.fromWebContents(event.sender)?.webContents;
  return contents ? contents.getZoomLevel() : 0;
});

if (process.platform === "win32") app.setAppUserModelId("Focus.Desktop");
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", (_event, argv) => {
    const showPanel = argv.includes("--show-patrol-panel");
    if (showPanel) revealPatrolPanel = true;
    if (patrolHost) {
      if (showPanel) patrolHost.openPanel();
      else patrolHost.restore();
      return;
    }
    if (!mainWindow || mainWindow.isDestroyed()) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
  });
  app.whenReady().then(start).catch(showFatalError);
  app.on("window-all-closed", () => { if (!patrolHost || fatalErrorShown) app.quit(); });
}
app.on("before-quit", event => {
  if (!fatalErrorShown && patrolHost?.hasUnconfirmedInput()) {
    event.preventDefault();
    patrolHost.requestQuit().catch(error => console.error("退出 Focus 失败:", error));
    return;
  }
  app.isQuitting = true;
  taskNotifications?.stop();
  startupAbort.abort();
  closeSplashWindow();
  patrolHost?.dispose();
  stopBackend();
});
app.on("will-quit", () => {
  if (fatalErrorShown) app.exit(1);
});
