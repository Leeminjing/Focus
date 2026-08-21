/*
 * 本文件启动 Focus 桌面运行时。输入为本机 Python/Git/Docker 能力与环境变量，输出为单一动态
 * loopback FastAPI 服务及从同一 Origin 加载的隔离 BrowserWindow；启动失败时输出本地说明窗口。
 */
const { app, BrowserWindow, dialog, ipcMain, Menu } = require("electron");
const { execFileSync, spawn } = require("node:child_process");
const crypto = require("node:crypto");
const net = require("node:net");
const path = require("node:path");

const desktopDir = __dirname;
const rootDir = path.resolve(desktopDir, "..");
const migrationIni = path.join(rootDir, "backend", "packages", "harness", "focus", "persistence", "migrations", "alembic.ini");
const python = process.env.FOCUS_PYTHON || "python";
let backend = null;

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
  const win = new BrowserWindow({ width: 760, height: 460, minWidth: 560, minHeight: 380, backgroundColor: "#f5f7fa" });
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
}

async function start() {
  Menu.setApplicationMenu(null);
  const session = crypto.randomBytes(32).toString("hex");
  const env = pythonEnvironment(session);
  checked(python, ["--version"]);
  checked("git", ["--version"]);
  checked("docker", ["compose", "version"]);
  // ponytail: 镜像已存在时不重复拉取（--pull missing），避免每次启动依赖镜像源网络
  checked("docker", ["compose", "-f", path.join(desktopDir, "compose.yaml"), "up", "-d", "--pull", "missing", "--wait"], { stdio: "inherit" });
  checked(python, ["-m", "alembic", "-c", migrationIni, "upgrade", "head"], { env, stdio: "inherit" });
  const port = await freePort();
  const apiBase = `http://127.0.0.1:${port}`;
  process.env.FOCUS_DESKTOP_API = apiBase;
  process.env.FOCUS_DESKTOP_SESSION = session;
  // 决策 1：桌面功能内嵌 Gateway，Electron 以 loopback 模式启动唯一 FastAPI 应用。
  // --loop 选择 Selector 事件循环（psycopg async 在 Windows 上不能用 ProactorEventLoop）
  backend = spawn(python, ["-m", "uvicorn", "backend.app.gateway.app:app", "--host", "127.0.0.1", "--port", String(port), "--loop", "backend.app.gateway.app:selector_loop_factory"], {
    cwd: rootDir,
    env: { ...env, FOCUS_DESKTOP_PORT: String(port) },
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
  });
  backend.stdout.on("data", data => process.stdout.write(data));
  backend.stderr.on("data", data => process.stderr.write(data));
  backend.once("exit", code => { if (code && !app.isQuitting) errorWindow(new Error(`FastAPI 已退出，代码 ${code}`)); });
  await waitForHealth(apiBase);
  const win = new BrowserWindow({
    width: 1440,
    height: 1024,
    minWidth: 900,
    minHeight: 680,
    autoHideMenuBar: true,
    backgroundColor: "#fbfbfc",
    webPreferences: {
      preload: path.join(desktopDir, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      // Chromium renderer isolation only; Python Agents still use real host paths with no Agent sandbox.
      sandbox: true,
    },
  });
  win.setMenuBarVisibility(false);
  // 决策 7：同源加载（页面与 API 同一 Origin，无需 CORS）
  await win.loadURL(`${apiBase}/desktop/`);
}

ipcMain.handle("focus:select-workspace", async () => {
  const result = await dialog.showOpenDialog({ properties: ["openDirectory", "createDirectory"] });
  return result.canceled ? "" : result.filePaths[0];
});

app.whenReady().then(start).catch(errorWindow);
app.on("window-all-closed", () => app.quit());
app.on("before-quit", () => {
  app.isQuitting = true;
  if (backend && !backend.killed) backend.kill();
});
