/*
 * 本文件启动 Focus 桌面运行时。输入为本机 Python/Git/Docker 能力、品牌资源与环境变量，输出为
 * 本地启动动画、单一动态 loopback FastAPI 及同源隔离主窗口；失败时原子切换到带品牌的说明窗口。
 */
const { app, BrowserWindow, dialog, ipcMain, Menu, shell } = require("electron");
const { execFileSync, spawn } = require("node:child_process");
const crypto = require("node:crypto");
const net = require("node:net");
const path = require("node:path");

const desktopDir = __dirname;
const rootDir = path.resolve(desktopDir, "..");
const migrationIni = path.join(rootDir, "backend", "packages", "harness", "focus", "persistence", "migrations", "alembic.ini");
const focusIconPath = path.join(desktopDir, "assets", process.platform === "win32" ? "focus-icon.ico" : "focus-icon.png");
let backend = null;
let splashWindow = null;
let mainWindow = null;
let fatalErrorShown = false;

if (process.env.FOCUS_DISABLE_HARDWARE_ACCELERATION === "1") {
  app.disableHardwareAcceleration();
  app.commandLine.appendSwitch("disable-gpu");
}
if (process.env.FOCUS_ELECTRON_USER_DATA) app.setPath("userData", process.env.FOCUS_ELECTRON_USER_DATA);

function checked(command, args, options = {}) {
  return execFileSync(command, args, { cwd: rootDir, encoding: "utf8", stdio: options.stdio || "pipe", env: options.env || process.env });
}

function pythonEnvironment(session) {
  const separator = process.platform === "win32" ? ";" : ":";
  const pythonPath = [path.join(rootDir, "backend", "packages", "harness"), rootDir, process.env.PYTHONPATH].filter(Boolean).join(separator);
  return {
    ...process.env,
    PYTHONPATH: pythonPath,
    OPENAI_API_KEY: process.env.OPENAI_API_KEY || "desktop-not-configured",
    JWT_SECRET: process.env.JWT_SECRET || "desktop-unused",
    FOCUS_DESKTOP_SESSION: session,
    FOCUS_DATABASE_URL: process.env.FOCUS_DATABASE_URL || "postgresql+asyncpg://focus:qweasdzxc123@127.0.0.1:7221/focus",
    PYTHONUTF8: "1",
  };
}

function pythonCandidates() {
  const candidates = [];
  const seen = new Set();
  const add = (command, prefix = []) => {
    if (!command) return;
    const key = `${command}\0${prefix.join("\0")}`.toLowerCase();
    if (seen.has(key)) return;
    seen.add(key);
    candidates.push({ command, prefix });
  };
  add(process.env.FOCUS_PYTHON);
  if (process.platform === "win32") {
    try {
      execFileSync("where.exe", ["python"], { encoding: "utf8", windowsHide: true })
        .split(/\r?\n/).map(value => value.trim()).filter(Boolean).forEach(value => add(value));
    } catch {}
    add("py", ["-3.12"]);
    add("py", ["-3"]);
  } else {
    add("python3");
  }
  add("python");
  return candidates;
}

function resolvePythonRuntime() {
  const failures = [];
  for (const candidate of pythonCandidates()) {
    try {
      execFileSync(candidate.command, [...candidate.prefix, "-c", "import alembic, uvicorn"], {
        cwd: rootDir,
        encoding: "utf8",
        stdio: "pipe",
        windowsHide: true,
      });
      return candidate;
    } catch (error) {
      failures.push(`${candidate.command} ${candidate.prefix.join(" ")}`.trim());
    }
  }
  throw new Error(`未找到同时包含 Alembic 与 Uvicorn 的 Python 运行时（已检查：${failures.join("、")}）`);
}

function checkedPython(runtime, args, options = {}) {
  return checked(runtime.command, [...runtime.prefix, ...args], options);
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
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${url}/health`);
      if (response.ok) return;
    } catch {}
    await new Promise(resolve => setTimeout(resolve, 350));
  }
  throw new Error("FastAPI 在 30 秒内未通过健康检查");
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
  win.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(page)}`);
  return win;
}

function showFatalError(error) {
  if (fatalErrorShown || app.isQuitting) return;
  fatalErrorShown = true;
  closeSplashWindow();
  errorWindow(error);
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
  await createSplashWindow();
  await updateSplashStage("正在检查本地运行环境", 10);
  const session = crypto.randomBytes(32).toString("hex");
  const env = pythonEnvironment(session);
  const pythonRuntime = resolvePythonRuntime();
  checkedPython(pythonRuntime, ["--version"]);
  checked("git", ["--version"]);
  checked("docker", ["compose", "version"]);
  await updateSplashStage("正在准备数据服务", 28);
  // ponytail: 镜像已存在时不重复拉取（--pull missing），避免每次启动依赖镜像源网络
  checked("docker", ["compose", "-f", path.join(desktopDir, "compose.yaml"), "up", "-d", "--pull", "missing", "--wait"], { stdio: "inherit" });
  await updateSplashStage("正在应用工作区迁移", 48);
  checkedPython(pythonRuntime, ["-m", "alembic", "-c", migrationIni, "upgrade", "head"], { env, stdio: "inherit" });
  const port = await freePort();
  const apiBase = `http://127.0.0.1:${port}`;
  process.env.FOCUS_DESKTOP_API = apiBase;
  process.env.FOCUS_DESKTOP_SESSION = session;
  await updateSplashStage("正在启动 Focus Gateway", 66);
  // 决策 1：桌面功能内嵌 Gateway，Electron 以 loopback 模式启动唯一 FastAPI 应用。
  // --loop 选择 Selector 事件循环（psycopg async 在 Windows 上不能用 ProactorEventLoop）
  backend = spawn(pythonRuntime.command, [...pythonRuntime.prefix, "-m", "uvicorn", "backend.app.gateway.app:app", "--host", "127.0.0.1", "--port", String(port), "--loop", "backend.app.gateway.app:selector_loop_factory"], {
    cwd: rootDir,
    env: { ...env, FOCUS_DESKTOP_PORT: String(port) },
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
  });
  backend.stdout.on("data", data => process.stdout.write(data));
  backend.stderr.on("data", data => process.stderr.write(data));
  backend.once("exit", (code, signal) => {
    backend = null;
    if (!app.isQuitting) showFatalError(new Error(`FastAPI 已退出（代码 ${code ?? "—"}${signal ? `，信号 ${signal}` : ""}）`));
  });
  await waitForHealth(apiBase);
  await updateSplashStage("正在加载工作区", 84);
  mainWindow = new BrowserWindow({
    show: false,
    width: 1440,
    height: 1024,
    minWidth: 900,
    minHeight: 680,
    autoHideMenuBar: true,
    titleBarStyle: "hidden",
    titleBarOverlay: {
      color: "#ffffff",
      symbolColor: "#18202d",
      height: 56,
    },
    backgroundColor: "#fbfbfc",
    icon: focusIconPath,
    webPreferences: {
      preload: path.join(desktopDir, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      // Chromium renderer isolation only; Python Agents still use real host paths with no Agent sandbox.
      sandbox: true,
    },
  });
  mainWindow.setMenuBarVisibility(false);
  protectAppNavigation(mainWindow, apiBase);
  // 决策 7：同源加载（页面与 API 同一 Origin，无需 CORS）
  await mainWindow.loadURL(`${apiBase}/desktop/`);
  await updateSplashStage("Focus 已就绪", 100);
  mainWindow.show();
  closeSplashWindow();
}

ipcMain.handle("focus:select-workspace", async () => {
  const result = await dialog.showOpenDialog({ properties: ["openDirectory", "createDirectory"] });
  return result.canceled ? "" : result.filePaths[0];
});

ipcMain.handle("focus:open-external", async (_event, value) => {
  const url = externalHttpUrl(value);
  if (!url) throw new Error("仅允许打开 HTTP(S) 外部链接");
  await shell.openExternal(url);
  return true;
});

if (process.platform === "win32") app.setAppUserModelId("Focus.Desktop");
app.whenReady().then(start).catch(showFatalError);
app.on("window-all-closed", () => app.quit());
app.on("before-quit", () => {
  app.isQuitting = true;
  closeSplashWindow();
  if (backend && !backend.killed) backend.kill();
});
